#!/usr/bin/env python3
"""Monitor script for XLM paper trading strategies.
Runs an infinite loop checking strategy performance every hour.
Every cycle it ranks *all* known strategies (running or stopped) by score,
stops anything ranked below KEEP_TOP_N, clones the top two with slightly
tweaked/revised thresholds, and makes sure the new clones plus the rest of the
top N are running. If the population is below KEEP_TOP_N (e.g. after strategies
were removed via `strat_manager.py rm`), it backfills the shortfall with fresh
clones from template_repo before scoring.
"""
import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import datetime
import random
import uuid
from pathlib import Path

from score import score_from_strategy_path

STATE_FILE = Path('/opt/strategy_state.json')
STRATEGIES_DIR = Path('/opt/strategies')
TRADES_DIR = Path('/opt/trades')
TEMPLATE_REPO = 'file:///opt/template_repo'
MASTER_AGENT_SCRIPT = Path('/opt/master_agent/master-agent.py')
# Override with the REVISION_TIMEOUT env var rather than editing this literal -- it has
# been flipped back and forth by successive emperor passes. The right value depends on
# which model master-agent.py is pointed at (see MASTER_AGENT_MODEL there): the cloud
# model fails or answers in minutes, the local ones can take hours.
REVISION_TIMEOUT = int(os.environ.get('REVISION_TIMEOUT', 60000))
KEEP_TOP_N = 8 # strategies ranked below this by net worth get stopped each cycle
RETIRE_BELOW_RANK = KEEP_TOP_N * 3 # stopped, never-traded strategies below this get untracked
LIVE_STRATEGY_FILE = Path('/opt/live_strategy.json') # which single strategy trades real money (pubnet-plan.md)

# Repos whose contents decide how real money moves. Watched every cycle by
# check_boundary_integrity(); see that function for why.
INTEGRITY_REPOS = [Path('/opt/tools'), Path('/opt/master_agent')]
INTEGRITY_BASELINE = Path('/opt/.integrity_baseline.json')

# Guardrails on handing a strategy the real-money live flag. On 2026-08-01 the live flag
# was given to a clone with zero trades and zero track record, purely because 45 clones
# that had never traded were tied at the top of a broken ranking. Rank alone is not
# enough evidence to trade real money on: require a demonstrated, profitable history.
MIN_LIVE_TRADES = 20        # trades logged before a strategy may go live
MIN_LIVE_AGE_S = 2 * 3600   # seconds between its first and last trade
MIN_LIVE_SCORE = 1000.0     # must actually be up on its starting balance

# How long a revised main.py gets to prove it runs and persists state before the gate
# gives up on it. Must comfortably exceed one loop iteration (the template fetches a
# price, then writes state.json, then sleeps 30s) plus a slow price-feed failover.
SMOKE_TEST_SECONDS = int(os.environ.get('SMOKE_TEST_SECONDS', 120))

PRICE_FETCH_ATTEMPTS = 3
PRICE_RETRY_DELAY = 60      # seconds between price-fetch attempts
PRICE_FAILURE_SLEEP = 300   # seconds to wait before retrying a cycle that got no price
CYCLE_SLEEP = 3600

def load_state():
    if STATE_FILE.exists():
        return json.load(STATE_FILE.open())
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def get_current_price():
    # Use the shared price_feed tool
    import sys as _sys
    _sys.path.append('/opt/tools')
    from price_feed import get_price
    return get_price()

def fetch_price_with_retry():
    # price_feed already tries 6 sources, but they all share one network path: when the
    # container's DNS flapped on 2026-08-01 every source failed at once. Retrying a few
    # times beats throwing away a whole hour of trading on a transient outage.
    for attempt in range(1, PRICE_FETCH_ATTEMPTS + 1):
        price = get_current_price()
        if price is not None:
            return price
        if attempt < PRICE_FETCH_ATTEMPTS:
            print(f'Price fetch failed (attempt {attempt}/{PRICE_FETCH_ATTEMPTS}); '
                  f'retrying in {PRICE_RETRY_DELAY}s')
            time.sleep(PRICE_RETRY_DELAY)
    return None

def compute_strategy_score(strategy_name, state_entry, price, marks=None):
    score = score_from_strategy_path(state_entry['path'], price, marks)
    if score is None:
        print(f'Error reading state for {strategy_name}')
        return -float('inf')
    return score

def trade_log_path(name):
    """Where `name`'s trades actually got logged.

    Normally /opt/trades/<name>.log, but trade_logger names the file after config.json's
    "name" field, which a revision is free to rewrite -- clone_916b729411ab logs to
    clone_916b729411ab_modified.log for exactly that reason. Fall back to whatever the
    config says so a renamed strategy doesn't look like it has never traded.
    """
    log_path = TRADES_DIR / f'{name}.log'
    if log_path.exists():
        return log_path
    try:
        entry = load_state().get(name)
        cfg = json.load(open(Path(entry['path']) / 'config.json'))
        alt = TRADES_DIR / f"{cfg['name']}.log"
        if alt.exists():
            return alt
    except Exception:
        pass
    return log_path

def trade_stats(name):
    """(trade_count, first_timestamp, last_timestamp) from this strategy's trade log.

    Used both to break ranking ties and to decide whether a strategy has enough of a
    track record to be trusted with the live pubnet flag. Returns zeros if the strategy
    has never traded, which is the common case for a fresh clone.
    """
    log_path = trade_log_path(name)
    if not log_path.exists():
        return 0, 0.0, 0.0
    first = last = 0.0
    count = 0
    try:
        with log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                count += 1
                try:
                    ts = json.loads(line).get('timestamp', 0.0)
                except Exception:
                    continue
                if not first:
                    first = ts
                last = ts
    except Exception as e:
        print(f'Could not read trade log for {name}: {e}')
    return count, first, last

def _tools():
    """Lazy handle on the /opt/tools modules, or None if unavailable.

    Imported lazily and defensively so that a problem in the asset stack degrades
    monitor to XLM-only behavior instead of taking down the whole culling loop.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import asset_discovery
        import dex_price
        import portfolio
        return portfolio, dex_price, asset_discovery
    except Exception as e:
        print(f'asset tooling unavailable ({e}); running XLM-only this cycle')
        return None


def fetch_marks_for_cycle(state, price):
    """One batch of USD marks for every asset any strategy declares or holds.

    Fetched once per cycle, before scoring. Scoring touches every tracked strategy, so
    doing network I/O per strategy would turn a ~40-strategy ranking pass into hundreds
    of blocking Horizon calls and earn a rate-limit.
    """
    marks = {'XLM': price}
    tools = _tools()
    if tools is None:
        return marks
    portfolio, dex_price, _ = tools

    specs = set()
    for name, entry in state.items():
        path = Path(entry.get('path', ''))
        try:
            cfg = json.load(open(path / 'config.json'))
            specs.update(a['spec'] for a in portfolio.assets_from_config(cfg))
        except Exception:
            pass
        try:
            st = portfolio.normalize_state(json.load(open(path / 'state.json')))
            specs.update(s for s, p in st['positions'].items()
                         if float(p.get('amount') or 0) > 0)
        except Exception:
            pass
    specs.discard('XLM')

    for spec, mark in dex_price.get_marks(sorted(specs)).items():
        if mark:
            marks[spec] = mark
    missing = sorted(specs - set(marks))
    if missing:
        print(f'  no mark for {len(missing)} asset(s): {", ".join(missing)}')
    return marks


ASSET_APPROVAL_TTL = 7 * 86400
VERIFIED_ASSETS_FILE = TRADES_DIR / '.verified_assets.json'


def _record_admission(code, issuer, verdict):
    """Write an admitted asset into the registry stellar_trader enforces against.

    This is the handoff between the second and third verification gates: monitor decides
    what is admissible, stellar_trader._verified_asset confirms nothing has changed
    before real money moves. Without this file being written, the enforcement gate finds
    no record and refuses every non-XLM trade -- safe, but it would mean the live path
    could never be enabled at all.

    Entries expire after ASSET_APPROVAL_TTL so an asset nobody has re-checked in a week
    stops being tradeable on its own. A `denied` record is never overwritten here:
    denials are permanent and are set by stellar_trader when a leg proves unsellable.
    """
    try:
        spec = __import__('assets').canonical(code, issuer)
    except Exception:
        return
    registry = {}
    if VERIFIED_ASSETS_FILE.exists():
        try:
            registry = json.loads(VERIFIED_ASSETS_FILE.read_text())
        except Exception:
            registry = {}

    if registry.get(spec, {}).get('denied'):
        return

    registry[spec] = {
        'code': code, 'issuer': issuer,
        'approved_at': time.time(),
        'approved_by': 'monitor',
        'expires_at': time.time() + ASSET_APPROVAL_TTL,
        'evidence': verdict.get('evidence', {}),
        'denied': False, 'deny_reason': None,
    }
    try:
        tmp = VERIFIED_ASSETS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(registry, indent=2))
        tmp.replace(VERIFIED_ASSETS_FILE)
    except Exception as e:
        print(f'  could not record admission for {spec}: {e}')


def _sanitize_assets(cfg_path, marks=None):
    """Re-verify every extra asset a config declares and delete the ones that fail.

    Runs on every clone, INCLUDING when the revision made no changes. The `assets` list
    is committed to git, so it propagates through `git clone` down the entire lineage
    and `apply_random_tweak` copies it verbatim -- without re-verification here, an
    asset denied in one generation keeps trading five generations later. Re-checking is
    also the only thing that catches an asset that was fine when admitted and has since
    been rugged, delisted, or drained of liquidity.

    Returns the surviving asset list. Rewrites config.json only if something changed.
    """
    tools = _tools()
    if tools is None:
        return []
    portfolio, _, asset_discovery = tools

    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return []

    # Iterate the RAW list, not portfolio.assets_from_config(). That helper dedupes by
    # asset code and caps the list, which is right for a *running* strategy but wrong
    # here: a second entry sharing a code with a good one would be silently skipped and
    # therefore never verified, and would stay in config.json on disk looking admitted.
    # Anything written into the file has to be examined here or physically removed.
    raw = cfg.get('assets')
    if not isinstance(raw, list) or not raw:
        return []

    kept, seen_codes = [], set()
    changed = False
    for entry in raw:
        if not isinstance(entry, dict):
            print('  dropping malformed asset entry (not an object)')
            changed = True
            continue
        code, issuer = entry.get('code'), entry.get('issuer')
        try:
            assets_mod = sys.modules.get('assets') or __import__('assets')
            spec = assets_mod.canonical(code, issuer)
        except Exception as e:
            print(f'  dropping asset {code!r}: {e}')
            changed = True
            continue
        # canonical('XLM', None) succeeds -- XLM is a valid asset, just never a valid
        # *extra* one. It is the permanent base leg carried by the top-level thresholds,
        # so listing it here would double-count the position.
        if assets_mod.is_native(spec):
            print('  dropping XLM from assets: it is the base leg, not an extra asset')
            changed = True
            continue
        if code.upper() in seen_codes:
            print(f'  dropping duplicate asset code {assets_mod.display(spec)}')
            changed = True
            continue
        if len(kept) >= portfolio.MAX_EXTRA_ASSETS:
            print(f'  dropping {code}: more than {portfolio.MAX_EXTRA_ASSETS} extra assets')
            changed = True
            continue

        verdict = asset_discovery.verify_asset(code, issuer)
        if not verdict['ok']:
            reason = verdict['vetoes'][0] if verdict['vetoes'] else \
                f"only {verdict['score']} evidence points from " \
                f"{verdict['sources_consulted']} sources"
            print(f'  dropping unverified asset {assets_mod.display(spec)} -- {reason}')
            changed = True
            continue

        seen_codes.add(code.upper())
        kept.append(entry)
        _record_admission(code, issuer, verdict)

    if changed:
        cfg['assets'] = kept
        try:
            json.dump(cfg, open(cfg_path, 'w'), indent=2)
        except Exception as e:
            print(f'  could not rewrite config after dropping assets: {e}')
    return portfolio.assets_from_config(cfg)


def _assets_are_sane(cfg, marks):
    """Validate the `assets` block. A missing mark is NOT a failure.

    Failing the whole config over a transient Horizon blip would send the revision to
    the random-tweak fallback and discard the model's work; _sanitize_assets drops
    unpriceable legs instead.
    """
    tools = _tools()
    if tools is None:
        return True
    portfolio = tools[0]

    raw = cfg.get('assets')
    if raw in (None, []):
        return True
    if not isinstance(raw, list) or len(raw) > portfolio.MAX_EXTRA_ASSETS:
        return False

    parsed = portfolio.assets_from_config(cfg)
    if len(parsed) != len(raw):
        return False       # something was malformed, duplicated, or was XLM

    for asset in parsed:
        mark = (marks or {}).get(asset['spec'])
        if not mark:
            continue       # unpriceable right now; _sanitize_assets handles it
        buy, sell = asset['buy_below'], asset['sell_above']
        if buy <= 0 or sell <= 0 or buy >= sell:
            return False
        if buy < mark * 0.5 or sell > mark * 1.5:
            return False
    return True


def _config_is_sane(cfg, price, marks=None):
    # Guards against a revision that "succeeds" (subprocess returncode 0) but
    # leaves the clone dead-on-arrival: unset/inverted thresholds never
    # trade, and thresholds set implausibly far from the real fetched price
    # (e.g. a stale training-data price assumption slipping through despite
    # being told the ground truth) will functionally never trade either.
    try:
        buy_below = float(cfg['buy_below'])
        sell_above = float(cfg['sell_above'])
    except (KeyError, TypeError, ValueError):
        return False
    if buy_below <= 0 or sell_above <= 0:
        return False
    if buy_below >= sell_above:
        return False
    if price and (buy_below < price * 0.5 or sell_above > price * 1.5):
        return False
    return _assets_are_sane(cfg, marks)

def main_py_is_sane(strategy_dir, name, price, marks=None):
    """Check that a revised main.py actually parses, runs, and persists state.

    _config_is_sane only ever looked at config.json, so a revision that gutted
    main.py sailed straight through and got started. On 2026-08-01 seed_0_1785619070
    was handed to a model that wrote main.py eight times, each attempt a syntax
    error, shrinking 3892 -> 377 bytes until the file finally *parsed* -- a stub
    with no trading loop that exits immediately and never writes state.json. It
    scored -inf (0 trades) forever after, and the config-only gate never noticed.

    Returns (ok, reason).

    The smoke run happens in a throwaway copy, never the real directory:
    trade_logger.execute_trade submits a REAL order when a live.flag exists in cwd,
    and record_trade appends to /opt/trades/<agent_name>.log -- which feeds the
    trade counts gating real-money promotion. So the copy drops live.flag and
    trades under a scratch name whose log is deleted afterwards.
    """
    main_py = Path(strategy_dir) / 'main.py'
    if not main_py.exists():
        return False, 'main.py is missing'

    source = main_py.read_text()
    try:
        ast.parse(source)
    except SyntaxError as e:
        return False, f'main.py has a syntax error: {e.msg} (line {e.lineno})'

    scratch_name = f'smoketest_{uuid.uuid4().hex[:12]}'
    tmp_root = tempfile.mkdtemp(prefix='smoketest_')
    tmp_dir = Path(tmp_root) / 'strategy'
    try:
        shutil.copytree(strategy_dir, tmp_dir,
                        ignore=shutil.ignore_patterns('.git', 'live.flag', 'state.json'))
        (tmp_dir / 'live.flag').unlink(missing_ok=True)  # belt and braces: never trade live

        cfg = {}
        try:
            cfg = json.load(open(tmp_dir / 'config.json'))
        except Exception:
            pass
        cfg['name'] = scratch_name
        if not _config_is_sane(cfg, price, marks):
            # Thresholds are validated separately; don't let a bad config mask a
            # main.py that would run fine once the fallback fixes them. The `assets`
            # list is deliberately preserved here -- stripping it would mean the smoke
            # test never exercises the multi-asset path it is supposed to be checking.
            cfg['buy_below'] = round(price * 0.98, 6)
            cfg['sell_above'] = round(price * 1.02, 6)
        json.dump(cfg, open(tmp_dir / 'config.json', 'w'), indent=2)

        # PAPER_ONLY makes the no-live-trading guarantee structural rather than relying
        # on having remembered to drop live.flag from the copy.
        env = dict(os.environ, PAPER_ONLY='1')
        proc = subprocess.Popen(['python3', '-u', 'main.py'], cwd=str(tmp_dir),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, preexec_fn=os.setsid, env=env)
        try:
            out, _ = proc.communicate(timeout=SMOKE_TEST_SECONDS)
            # Exiting on its own is fatal: strat_manager starts main.py once, so a
            # strategy that returns is simply dead and can never trade again.
            tail = ' | '.join(out.strip().splitlines()[-3:]) or '(no output)'
            return False, (f'main.py exited on its own after '
                           f'<{SMOKE_TEST_SECONDS}s (rc={proc.returncode}): {tail}')
        except subprocess.TimeoutExpired:
            pass  # still running, which is what a trading loop should do
        finally:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                out = ''

        state_path = tmp_dir / 'state.json'
        if not state_path.exists():
            tail = ' | '.join((out or '').strip().splitlines()[-3:]) or '(no output)'
            return False, (f'main.py ran for {SMOKE_TEST_SECONDS}s without ever writing '
                           f'state.json: {tail}')
        try:
            raw = json.load(open(state_path))
        except Exception as e:
            return False, f'main.py wrote an unreadable state.json: {e}'

        tools = _tools()
        if tools is None:
            usd = float(raw.get('balance_usd', 0.0))
            xlm = float(raw.get('balance_xlm', 0.0))
            if usd < 0 or xlm < 0:
                return False, f'main.py wrote negative balances (usd={usd}, xlm={xlm})'
            if usd + xlm * price <= 0:
                return False, f'main.py wrote a zero/negative net worth (usd={usd}, xlm={xlm})'
            return True, f'ran {SMOKE_TEST_SECONDS}s, net worth {usd + xlm * price:.2f}'

        portfolio = tools[0]
        st = portfolio.normalize_state(raw)
        usd = st['balance_usd']
        if usd < 0:
            return False, f'main.py wrote a negative USD balance ({usd})'
        for spec, position in st['positions'].items():
            if float(position.get('amount') or 0) < 0:
                return False, f'main.py wrote a negative {spec} balance'

        # A position in something the config does not declare means the strategy
        # traded an asset that was never admitted -- exactly what the asset gate
        # exists to prevent, so it fails the revision rather than being tidied away.
        declared = portfolio.declared_specs(cfg)
        undeclared = set(st['positions']) - declared
        undeclared = {s for s in undeclared
                      if float(st['positions'][s].get('amount') or 0) > 0}
        if undeclared:
            return False, f'main.py holds undeclared asset(s): {", ".join(sorted(undeclared))}'

        marks_for_check = dict(marks or {})
        marks_for_check.setdefault('XLM', price)
        net_worth, unpriced = portfolio.net_worth(st, marks_for_check)
        if net_worth <= 0:
            return False, f'main.py wrote a zero/negative net worth ({net_worth})'
        note = f', {len(unpriced)} leg(s) unpriced' if unpriced else ''
        return True, f'ran {SMOKE_TEST_SECONDS}s, net worth {net_worth:.2f}{note}'
    except Exception as e:
        return False, f'smoke test errored: {e}'
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        (TRADES_DIR / f'{scratch_name}.log').unlink(missing_ok=True)

def revert_main_py(strategy_dir, before_head, name):
    """Restore the parent's main.py after a failed gate, keeping any other revision.

    Reverts to the commit the clone started at rather than HEAD, since the model may
    have already committed the broken main.py onto its own branch.
    """
    r = _git(strategy_dir, 'checkout', before_head, '--', 'main.py')
    if r.returncode != 0:
        print(f'Could not restore main.py for {name}: {r.stderr.strip()}')
        return False
    print(f"Restored {name}'s main.py to its parent's version")
    return True

# Files a strategy rewrites constantly at runtime, which must never be committed:
# main.py rewrites state.json every 30s, and master-agent.py drops its revision
# transcript next to it. Clones descended from a pre-2026-07-31 ancestor predate the
# template's .gitignore and don't list either, so `git add -A` below would commit
# them and leave the repo permanently dirty from the next tick onward.
REVISION_IGNORES = ('state.json', '.strategy-revision-history.json')

def _git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True)

def _git_head(path):
    r = _git(path, 'rev-parse', 'HEAD')
    return r.stdout.strip() if r.returncode == 0 else None

def _git_is_dirty(path):
    r = _git(path, 'status', '--porcelain')
    return bool(r.stdout.strip()) if r.returncode == 0 else False

def revision_changed_anything(path, before_head):
    """True if anything at all happened to `path` since it was cloned.

    A revise-strategy subprocess can exit 0 having done nothing (the model
    answers in prose and never calls a tool), which used to start the clone as a
    byte-identical copy of its parent -- a wasted revision slot indistinguishable
    from a successful one in the logs.
    """
    return _git_is_dirty(path) or _git_head(path) != before_head

def _ensure_revision_gitignore(path):
    gi = Path(path) / '.gitignore'
    text = gi.read_text() if gi.exists() else ''
    missing = [p for p in REVISION_IGNORES if p not in text.split()]
    if not missing:
        return
    if text and not text.endswith('\n'):
        text += '\n'
    gi.write_text(text + '\n'.join(missing) + '\n')

def commit_revision(path, name, message):
    """Commit whatever is in `path`'s working tree, on its own branch.

    monitor.py judged a revision solely by the subprocess exit code and
    _config_is_sane, neither of which notices that the model edited files and
    never ran git -- the commit requirement lived only as prompt text in
    master-agent.py, and was ignored often enough that most strategies on disk
    sat on master with an uncommitted config.json. That doesn't hurt the clone
    itself (main.py reads the working tree) but it silently breaks the next
    generation: strat_manager.py clones a parent with plain `git clone`, which
    copies committed history only, so the winning thresholds are replaced by
    whatever HEAD still holds -- usually the template's never-trading 0.0s.

    Commit unconditionally, whoever wrote the change (LLM or a tweak fallback),
    so the working tree and HEAD can never disagree. Returns True if `path` ends
    up clean and committed.
    """
    if not (Path(path) / '.git').exists():
        print(f'Cannot commit revision for {name}: {path} is not a git repo')
        return False

    _ensure_revision_gitignore(path)

    # Keep the branch the model made if it made one; otherwise put the revision on
    # its own branch so the parent's master stays a clean ancestor line. `git clone`
    # follows the source repo's checked-out HEAD, so either way children inherit it.
    branch = _git(path, 'branch', '--show-current').stdout.strip()
    if branch in ('master', 'main', ''):
        new_branch = f'auto/{name}_{int(time.time())}'
        r = _git(path, 'checkout', '-b', new_branch)
        if r.returncode != 0:
            print(f'Could not branch {name} to {new_branch}: {r.stderr.strip()}')

    if not _git_is_dirty(path):
        return True  # model already committed everything it touched

    _git(path, 'add', '-A')
    commit_args = ['commit', '-m', message]
    if not _git(path, 'config', 'user.email').stdout.strip():
        # Fresh container: git has no identity configured and commit would abort.
        commit_args = ['-c', 'user.email=monitor@localhost',
                       '-c', 'user.name=monitor.py'] + commit_args
    r = _git(path, *commit_args)
    if r.returncode != 0:
        print(f'Failed to commit revision for {name}: {r.stderr.strip() or r.stdout.strip()}')
        return False
    print(f'Committed revision for {name} on branch '
          f'{_git(path, "branch", "--show-current").stdout.strip()}')
    return True

def apply_seed_thresholds(cfg_path, name, price):
    # Used only for bootstrapping: the template's config.json ships with
    # buy_below=sell_above=0.0, which never trades and can't be nudged by
    # apply_random_tweak (0.0 * anything is still 0.0). Seed a real range
    # around the current price instead.
    trade_amount_usd = 10.0
    if cfg_path.exists():
        try:
            trade_amount_usd = json.load(open(cfg_path)).get('trade_amount_usd', 10.0)
        except Exception:
            pass
    new_cfg_data = {
        'name': name,
        'schema_version': 2,
        'buy_below': round(price * 0.98, 6),
        'sell_above': round(price * 1.02, 6),
        'trade_amount_usd': trade_amount_usd,
        # Seeds are always XLM-only. Picking extra assets is a trait the population
        # evolves via revisions, not something bootstrapping guesses at.
        'assets': [],
    }
    json.dump(new_cfg_data, open(cfg_path, 'w'), indent=2)

def bootstrap_initial_strategies(price, count=2, marks=None):
    # First run: strategy_state.json doesn't exist yet, so there's nothing to
    # stop or clone-from in the normal cycle below. Seed `count` strategies
    # straight from the template so there's something for later cycles to evolve.
    print(f'No strategies registered yet; bootstrapping {count} initial strategies from the template.')
    for i in range(count):
        name = f'seed_{i}_{int(time.time())}'
        print(f'Bootstrapping strategy {name}')
        subprocess.run(['/opt/strat_manager.py', 'clone', name, TEMPLATE_REPO])
        strategy_dir = STRATEGIES_DIR / name
        cfg_path = strategy_dir / 'config.json'
        before_head = _git_head(strategy_dir)

        revised = False
        if MASTER_AGENT_SCRIPT.exists():
            try:
                result = subprocess.run(
                    ['python3', str(MASTER_AGENT_SCRIPT), 'revise-strategy',
                     name, name, '1000.0', '{}', str(price)],
                    capture_output=True, text=True, timeout=REVISION_TIMEOUT,
                )
                if result.returncode == 0:
                    print(f'Master agent revised {name}:\n{result.stdout.strip()}')
                    revised = True
                else:
                    print(f'Master agent revision failed for {name}: {result.stderr.strip()}')
            except Exception as e:
                print(f'Master agent revision errored for {name}: {e}')

        touched = revision_changed_anything(strategy_dir, before_head)
        if revised and not touched:
            print(f'Revision for {name} left the clone untouched; treating as unrevised')
            revised = False

        # Runs whenever the model touched the repo, even if `revised` is already False:
        # both fallbacks only rewrite config.json, so a gutted main.py would otherwise
        # survive a failed config check and get started anyway (seed_0_1785619070).
        # Verify declared assets before the smoke test, so the smoke run exercises the
        # same asset list the strategy will actually start with. Runs even when the
        # revision failed: the fallbacks copy `assets` through untouched.
        if cfg_path.exists():
            _sanitize_assets(cfg_path, marks)

        if touched:
            ok, reason = main_py_is_sane(strategy_dir, name, price, marks)
            print(f'main.py check for {name}: {"passed" if ok else "FAILED"} -- {reason}')
            if not ok:
                revert_main_py(strategy_dir, before_head, name)
                revised = False

        if revised and cfg_path.exists():
            cfg = json.load(open(cfg_path))
            if not _config_is_sane(cfg, price, marks):
                print(f'Revised config for {name} failed sanity check ({cfg}); treating as unrevised')
                revised = False

        if not revised and cfg_path.exists():
            print(f'Falling back to seeded thresholds for {name}')
            apply_seed_thresholds(cfg_path, name, price)

        commit_revision(strategy_dir, name,
                        f'auto: seed {name} ({"llm" if revised else "seeded thresholds"})')

        subprocess.run(['/opt/strat_manager.py', 'start', name])

def apply_random_tweak(parent_cfg_path, new_cfg_path, new_name):
    # Fallback used when the master agent is unavailable or fails: copy the parent's
    # config with thresholds nudged by a small random factor (e.g. 0.95 - 1.05).
    parent = json.load(open(parent_cfg_path))
    tweak = random.uniform(0.95, 1.05)
    new_cfg_data = {
        'name': new_name,
        'schema_version': parent.get('schema_version', 2),
        'buy_below': round(parent['buy_below'] * tweak, 6),
        'sell_above': round(parent['sell_above'] * tweak, 6),
        'trade_amount_usd': parent.get('trade_amount_usd', 10.0)
    }

    # Carry the parent's extra assets across, nudging each leg by its OWN factor so the
    # fallback explores per-leg thresholds rather than moving every leg in lockstep.
    # Never invents an asset: only assets the parent already had (and which
    # _sanitize_assets re-verifies afterwards) can appear here.
    inherited = []
    for asset in (parent.get('assets') or []):
        if not isinstance(asset, dict):
            continue
        leg_tweak = random.uniform(0.95, 1.05)
        leg = dict(asset)
        for key in ('buy_below', 'sell_above'):
            try:
                leg[key] = round(float(asset[key]) * leg_tweak, 9)
            except (KeyError, TypeError, ValueError):
                pass
        inherited.append(leg)
    if inherited:
        new_cfg_data['assets'] = inherited

    json.dump(new_cfg_data, open(new_cfg_path, 'w'), indent=2)

def _config_signature(state_entry):
    """The parameters that actually define a strategy's behaviour, for dedup."""
    try:
        cfg = json.load(open(Path(state_entry['path']) / 'config.json'))
    except Exception:
        return None
    assets = cfg.get('assets') or []
    if not isinstance(assets, list):
        assets = []
    # Assets are part of what defines a strategy. Without them two clones differing
    # ONLY in which assets they picked look identical here, select_parents discards one
    # as a duplicate, and a revision slot is wasted -- killing exactly the diversity
    # multi-asset support exists to create.
    asset_sig = tuple(sorted(
        (a.get('code'), a.get('issuer'), a.get('buy_below'), a.get('sell_above'))
        for a in assets if isinstance(a, dict)))
    return (cfg.get('buy_below'), cfg.get('sell_above'),
            cfg.get('trade_amount_usd'), asset_sig)

def select_parents(performances, state, count=2):
    """Pick `count` distinct strategies to clone and revise this cycle.

    Taking performances[:count] outright kept picking two clones with byte-identical
    configs (the monitor logs show the same one or two parents cloned hour after hour),
    which spends both revision slots exploring the same point. Prefer the best scorer,
    then the best remaining one whose config actually differs; fall back to plain rank
    order if everything looks the same.
    """
    chosen = []
    seen_signatures = []
    for name, score in performances:
        if name not in state:
            continue
        signature = _config_signature(state[name])
        if signature is not None and signature in seen_signatures:
            continue
        chosen.append((name, score))
        seen_signatures.append(signature)
        if len(chosen) == count:
            return chosen
    # Not enough distinct configs -- top up by rank so a cycle is never skipped.
    for entry in performances:
        if entry not in chosen and entry[0] in state:
            chosen.append(entry)
            if len(chosen) == count:
                break
    return chosen

def load_live_strategy():
    if LIVE_STRATEGY_FILE.exists():
        try:
            return json.load(LIVE_STRATEGY_FILE.open())
        except Exception:
            return None
    return None

def save_live_strategy(name):
    LIVE_STRATEGY_FILE.write_text(json.dumps({'name': name, 'since': time.time()}, indent=2))

def set_live_flag(name, live):
    # Plain marker file main.py polls for, not a config.json key, so it survives a
    # revision rewriting config.json from scratch. monitor.py alone writes/deletes it
    # — a strategy's own code never decides it's live (pubnet-plan.md).
    flag_path = STRATEGIES_DIR / name / 'live.flag'
    if live:
        flag_path.touch()
    else:
        flag_path.unlink(missing_ok=True)

def main_py_calls_execute_trade(name):
    """Whether <name>/main.py routes its trades through trade_logger.execute_trade.

    About ten strategies in the population still mutate balances inline and call
    record_trade directly -- the style the template moved away from. They log paper
    trades normally and are indistinguishable on the leaderboard, but execute_trade is
    the *only* path that checks live.flag and calls stellar_trader.submit_trade, so
    those strategies are structurally incapable of placing a real order.

    Promoting one is worse than a no-op: set_live_flag writes live.flag, the strategy
    ignores it, and monitor now believes real money is deployed behind a position that
    was never opened. The next leader change then calls wind_down() against an account
    this strategy never touched, and gates the promotion on liquidating a position that
    doesn't exist.

    Returns (ok, reason). An unreadable or unparseable main.py fails closed.
    """
    main_py = STRATEGIES_DIR / name / 'main.py'
    try:
        tree = ast.parse(main_py.read_text())
    except Exception as e:
        return False, f'could not parse main.py ({e})'
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
        if called == 'execute_trade':
            return True, ''
    return False, ('main.py never calls execute_trade (inline record_trade style), '
                   'so it cannot submit a real order')


def check_boundary_integrity():
    """Has the real-money safety boundary been modified out from under us? (ok, reasons)

    stellar_trader.py is the only thing enforcing MAX_TRADE_USD / MAX_DAILY_USD, and its
    docstring says those caps are deliberately not caller-supplied so a revision cannot
    override them. That argument holds only as long as the file itself is fixed -- and
    the revision agent runs `exec` as root with no path restriction and is explicitly
    told it may edit its own tooling. Nothing structurally prevents it rewriting the caps
    or the trading code. Until that hole is closed properly (an unprivileged revision
    user, or a read-only mount), this is the detection layer: notice when the boundary
    changes and stop trading real money rather than trusting it.

    Two signals, both cheap:
      * a dirty working tree in /opt/tools or /opt/master_agent -- the realistic shape of
        a tampering event, since a model writing a file has no reason to commit it
      * HEAD moved away from the recorded baseline -- catches the case where it does

    The baseline lives in INTEGRITY_BASELINE and is adopted automatically the first time
    this runs against clean repos. After a legitimate change, commit it and delete that
    file; the next cycle re-adopts. That deliberate manual step is the point -- an
    automatic re-baseline would detect nothing.
    """
    reasons = []
    heads = {}
    for repo in INTEGRITY_REPOS:
        if not (repo / '.git').exists():
            continue
        # Deliberately NOT .strip()ed: porcelain status codes occupy two fixed columns
        # and an unstaged modification is " M path", so stripping the whole output eats
        # the leading space of the first line and shifts every column offset by one
        # (reported "onitor.py" for monitor.py).
        dirty = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain'],
                               capture_output=True, text=True).stdout
        head = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True).stdout.strip()
        heads[str(repo)] = head
        changed = [line[3:].strip() for line in dirty.splitlines() if line.strip()]
        if changed:
            shown = ', '.join(changed[:5])
            more = f' +{len(changed) - 5} more' if len(changed) > 5 else ''
            reasons.append(f'{repo} has uncommitted changes ({shown}{more})')

    baseline = {}
    if INTEGRITY_BASELINE.exists():
        try:
            baseline = json.loads(INTEGRITY_BASELINE.read_text())
        except Exception:
            reasons.append('integrity baseline is unreadable')

    if baseline:
        for repo, head in heads.items():
            recorded = baseline.get(repo)
            if recorded and head and recorded != head:
                reasons.append(
                    f'{repo} HEAD moved {recorded[:8]} -> {head[:8]} without re-baselining')
    elif not reasons:
        # First clean run: adopt what we see. Never adopt a dirty tree as the baseline,
        # or a tampering event present at startup would be blessed permanently.
        try:
            INTEGRITY_BASELINE.write_text(json.dumps(heads, indent=2))
            print(f'Recorded live-trading integrity baseline: '
                  f'{ {k: v[:8] for k, v in heads.items()} }')
        except Exception as e:
            print(f'Could not write integrity baseline: {e}')

    return (not reasons), reasons


def qualifies_for_live(name, score):
    """Whether `name` has earned the right to trade real money. (ok, reason)

    Being ranked #1 is necessary but nowhere near sufficient: a brand-new clone that
    has never traded sits at exactly its starting balance, which can top the ranking
    on any quiet day. Real money only goes to a strategy with a real, profitable
    track record. If nothing qualifies, nothing is promoted and no live trading
    happens this cycle -- that is the intended safe default.
    """
    # Checked first: this is about whether the strategy *can* trade live at all, which
    # no amount of paper track record makes up for.
    can_execute, why_not = main_py_calls_execute_trade(name)
    if not can_execute:
        return False, why_not
    count, first, last = trade_stats(name)
    if count < MIN_LIVE_TRADES:
        return False, f'only {count} trades logged (need {MIN_LIVE_TRADES})'
    age = last - first
    if age < MIN_LIVE_AGE_S:
        return False, f'trade history spans only {age / 3600:.1f}h (need {MIN_LIVE_AGE_S / 3600:.1f}h)'
    if score <= MIN_LIVE_SCORE:
        return False, f'score {score:.2f} is not above the {MIN_LIVE_SCORE:.2f} starting baseline'
    return True, f'{count} trades over {age / 3600:.1f}h, score {score:.2f}'

def promote_live_strategy(current_leader, leader_score):
    """Ensure `current_leader` (this cycle's #1 by score) is the one strategy
    marked live, provided it passes qualifies_for_live(). On a leader change, the flip
    is gated on stellar_trader.wind_down() fully liquidating the outgoing strategy's
    real pubnet position first — if it can't finish in one cycle, the old leader simply
    stays live and this retries next cycle (safe: run()'s cull loop exempts the live
    strategy from KEEP_TOP_N below).
    """
    live = load_live_strategy()

    # Integrity is checked before the "already live" early return, so a boundary change
    # halts an incumbent too -- not just a would-be promotion.
    intact, problems = check_boundary_integrity()
    if not intact:
        print('LIVE TRADING HALTED -- the real-money safety boundary has changed:')
        for problem in problems:
            print(f'  - {problem}')
        held = live.get('name') if live else None
        if held:
            # Clear the flag so no further real orders can be placed, but deliberately
            # do NOT wind_down: liquidating would route through the very code whose
            # integrity is in question. Stopping is safe and reversible; trading through
            # possibly-modified caps is not. Any open position is left for a human.
            set_live_flag(held, False)
            print(f'  cleared live.flag on {held}; its position (if any) is left open '
                  f'for manual review')
        print('  review the changes, commit them, then delete '
              f'{INTEGRITY_BASELINE} to re-baseline')
        return

    if live and live.get('name') == current_leader:
        # Re-assert the flag rather than just returning. A halt above clears live.flag
        # while leaving live_strategy.json pointing at the same name, so this early
        # return would otherwise never restore it: the system would stay halted forever
        # even after the operator fixed the boundary and re-baselined, with no path back
        # short of a leader change. Recovery should follow integrity automatically.
        flag = STRATEGIES_DIR / current_leader / 'live.flag'
        if not flag.exists():
            print(f'Restoring live.flag for {current_leader} (boundary intact again)')
            set_live_flag(current_leader, True)
        return

    ok, reason = qualifies_for_live(current_leader, leader_score)
    if not ok:
        held = live.get('name') if live else 'nothing'
        print(f'Not promoting {current_leader} to live: {reason}. Leaving {held} live.')
        return
    print(f'{current_leader} qualifies for live: {reason}')

    if live and live.get('name'):
        old_name = live['name']
        print(f'Live strategy changing from {old_name} to {current_leader}; winding down {old_name} first')
        import sys as _sys
        _sys.path.append('/opt/tools')
        from stellar_trader import wind_down
        result = wind_down()
        if not result['liquidated']:
            print(f"wind_down did not fully flatten {old_name} this cycle "
                  f"(remaining_xlm={result['remaining_xlm']}, reason={result['reason']}); "
                  f"{old_name} stays live, retrying next cycle")
            return
        print(f"wind_down flattened {old_name} ({result['chunks']} chunk(s))")
        set_live_flag(old_name, False)

    print(f'Promoting {current_leader} to live')
    set_live_flag(current_leader, True)
    save_live_strategy(current_leader)

def run():
    while True:
        print('--- Monitoring cycle', datetime.datetime.now(), '---')
        price = fetch_price_with_retry()
        if price is None:
            print(f'Could not fetch price after {PRICE_FETCH_ATTEMPTS} attempts; '
                  f'retrying this cycle in {PRICE_FAILURE_SLEEP}s')
            time.sleep(PRICE_FAILURE_SLEEP)
            continue

        # Correct any strategy left with a stale 'running' status from a
        # crash between cycles (nothing else ever notices this), so the
        # restart-self-heal loop below can pick it back up if it's top-N.
        subprocess.run(['/opt/strat_manager.py', 'reconcile'])
        # Drop clones that were cloned but never actually started (e.g. monitor.py
        # got killed mid-cycle between the clone and start steps) -- these can
        # never be scored and would otherwise print "Error reading state" and
        # sort to -inf every cycle forever.
        subprocess.run(['/opt/strat_manager.py', 'prune'])

        state = load_state()
        # One batch of asset marks per cycle, before any scoring. Doing this per
        # strategy would mean hundreds of blocking Horizon calls per ranking pass.
        marks = fetch_marks_for_cycle(state, price)
        if not state:
            bootstrap_initial_strategies(price, marks=marks)
            print(f'Sleeping for {CYCLE_SLEEP}s...')
            time.sleep(CYCLE_SLEEP)
            continue

        if len(state) < KEEP_TOP_N:
            shortfall = KEEP_TOP_N - len(state)
            print(f'Only {len(state)} strategies known (< {KEEP_TOP_N}); backfilling {shortfall} from template.')
            bootstrap_initial_strategies(price, count=shortfall, marks=marks)
            state = load_state()

        performances = []
        trade_counts = {}
        for name, info in state.items():
            score = compute_strategy_score(name, info, price, marks)
            trade_counts[name] = trade_stats(name)[0]
            performances.append((name, score))
        # Tie-break on trade count. Strategies that have never traded all sit at exactly
        # their starting balance, so without this the ordering of a 45-way tie is just
        # the insertion order of strategy_state.json -- the same names won and lost every
        # cycle, and the "best" strategy handed to the revision agent was arbitrary.
        # Among equals, prefer the one that has actually demonstrated something.
        performances.sort(key=lambda x: (x[1], trade_counts.get(x[0], 0)), reverse=True)
        print('Strategy performances (score):')
        for name, score in performances:
            print(f'  {name}: {score:.2f} ({trade_counts.get(name, 0)} trades)')

        current_leader, leader_score = performances[0]
        promote_live_strategy(current_leader, leader_score)
        live_state = load_live_strategy()
        live_name = live_state['name'] if live_state else None

        # Stop everything ranked below KEEP_TOP_N (no-op if already
        # stopped). Re-derived from full-population rank every cycle, so a
        # stopped strategy can't get stuck occupying a "cull" slot forever the
        # way a fixed "worst two" window could. The live strategy is exempt even if
        # it falls out of the top N — it must not be killed while holding a real
        # pubnet position (pubnet-plan.md).
        for name, _ in performances[KEEP_TOP_N:]:
            info = state.get(name)
            if info and info.get('status') == 'running':
                if name == live_name:
                    print(f'Skipping cull for {name}: currently the live pubnet strategy')
                    continue
                print(f'Stopping strategy below rank {KEEP_TOP_N}: {name}')
                subprocess.run(['/opt/strat_manager.py', 'stop', name])

        # Two clones are created every cycle and nothing ever removed them, so the
        # tracked population grew ~2/hour forever (74 tracked / 88 directories by
        # 2026-08-01) and every cycle re-scored dozens of identical never-traded
        # clones. Untrack the deep tail: stopped, never traded, nothing to learn from.
        # Files stay on disk; the live strategy and anything with a trade history are
        # never touched.
        retire_state = load_state()
        for name, _ in performances[RETIRE_BELOW_RANK:]:
            info = retire_state.get(name)
            if not info or info.get('status') != 'stopped' or info.get('pid'):
                continue
            if name == live_name or trade_counts.get(name, 0) > 0:
                continue
            print(f'Retiring never-traded strategy ranked below {RETIRE_BELOW_RANK}: {name}')
            subprocess.run(['/opt/strat_manager.py', 'rm', name])

        # Clone the top two and hand each clone to the master agent to revise
        top_two = select_parents(performances, state)
        leaderboard = json.dumps({n: score for n, score in performances})
        new_clone_names = []
        for name, score in top_two:
            # Only the name is intentionally not derived from the parent (so it
            # doesn't keep growing across generations of clones-of-clones); the
            # git clone itself still pulls the winning strategy's own repo.
            new_name = f"clone_{uuid.uuid4().hex[:12]}"
            parent_repo = state[name]['path']
            print(f'Cloning best strategy {name} as {new_name}')
            subprocess.run(['/opt/strat_manager.py', 'clone', new_name, parent_repo])
            new_clone_names.append(new_name)
            parent_cfg = Path(parent_repo) / 'config.json'
            new_dir = Path(STRATEGIES_DIR) / new_name
            new_cfg = new_dir / 'config.json'
            before_head = _git_head(new_dir)

            revised = False
            if MASTER_AGENT_SCRIPT.exists():
                try:
                    result = subprocess.run(
                        ['python3', str(MASTER_AGENT_SCRIPT), 'revise-strategy',
                         new_name, name, str(score), leaderboard, str(price)],
                        capture_output=True, text=True, timeout=REVISION_TIMEOUT,
                    )
                    if result.returncode == 0:
                        print(f'Master agent revised {new_name}:\n{result.stdout.strip()}')
                        revised = True
                    else:
                        print(f'Master agent revision failed for {new_name}: {result.stderr.strip()}')
                except Exception as e:
                    print(f'Master agent revision errored for {new_name}: {e}')

            touched = revision_changed_anything(new_dir, before_head)
            if revised and not touched:
                print(f'Revision for {new_name} left the clone identical to {name}; treating as unrevised')
                revised = False

            # Always re-verify declared assets, revised or not: `assets` is committed
            # to git and inherited by every future clone of this lineage, and
            # apply_random_tweak copies it forward verbatim.
            if new_cfg.exists():
                _sanitize_assets(new_cfg, marks)

            # See bootstrap_initial_strategies: this must run even when `revised` is
            # already False, since the random-tweak fallback only rewrites config.json.
            if touched:
                ok, reason = main_py_is_sane(new_dir, new_name, price, marks)
                print(f'main.py check for {new_name}: {"passed" if ok else "FAILED"} -- {reason}')
                if not ok:
                    revert_main_py(new_dir, before_head, new_name)
                    revised = False

            if revised and new_cfg.exists():
                cfg = json.load(open(new_cfg))
                if not _config_is_sane(cfg, price, marks):
                    print(f'Revised config for {new_name} failed sanity check ({cfg}); treating as unrevised')
                    revised = False

            if not revised and parent_cfg.exists() and new_cfg.exists():
                print(f'Falling back to random tweak for {new_name}')
                apply_random_tweak(parent_cfg, new_cfg, new_name)

            commit_revision(new_dir, new_name,
                            f'auto: revise {new_name} from {name} '
                            f'({"llm" if revised else "random tweak"})')

        # Start the new clones, plus any top-N strategy that's currently stopped
        # (reload state first since strat_manager.py clone/stop wrote to disk)
        for new_name in new_clone_names:
            subprocess.run(['/opt/strat_manager.py', 'start', new_name])

        fresh_state = load_state()
        for name, _ in performances[:KEEP_TOP_N]:
            info = fresh_state.get(name)
            if info and info.get('status') == 'stopped':
                print(f'Restarting top-{KEEP_TOP_N} strategy that was stopped: {name}')
                subprocess.run(['/opt/strat_manager.py', 'start', name])

        # Wait an hour before next cycle
        print(f'Sleeping for {CYCLE_SLEEP}s...')
        time.sleep(CYCLE_SLEEP)

if __name__ == '__main__':
    run()

#!/usr/bin/env python3
"""Monitor script for XLM paper trading strategies.
Runs an infinite loop checking strategy performance every hour.
Every cycle it ranks *all* known strategies (running or stopped) by score,
stops anything ranked below KEEP_TOP_N, and adds three newcomers: CLONES_PER_CYCLE
clones of the best distinct performers plus TEMPLATE_SPAWNS_PER_CYCLE pulled fresh
from template_repo, all with slightly tweaked/revised thresholds. It then makes sure
those plus the rest of the top N are running -- so the rank-based cull stops three per
cycle simply because three are added. Up to REVISIONS_PER_CYCLE of the newcomers are
handed to the LLM (and only on REVISION_CHANCE of cycles) -- currently all of them --
each with a prompt matching its role, ROLE_REFINE for a clone or ROLE_EXPLORE for a
template spawn; see _revision_budget and the ROLE_* constants.
If the population is below KEEP_TOP_N (e.g. after strategies were removed via
`strat_manager.py rm`), it backfills the shortfall from template_repo before scoring.

To stop it, touch /opt/.monitor.py.exit: it exits at its next between-cycle sleep
rather than being signalled mid-revision (see EXIT_FILE / sleep_or_exit below).
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
# One definition of "which python runs a strategy", shared with the process that actually
# starts them, so the smoke test can never gate on a different environment than the run.
from strat_manager import _strategy_python

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
LIVE_STRATEGY_FILE = Path('/opt/live_strategy.json') # which single strategy trades real money

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

# Whether a revised main.py has to stay *importable by the backtester*, i.e. whether
# backtest.py can load a top-level decide() out of it. If it can't, backtest_strategy
# silently replays config.json's plain buy_below/sell_above rule and reports
# decide_source: 'config-thresholds' -- so beats_buy_hold, return_pct and win_rate are
# confident-looking numbers about something other than the code being ranked, and the
# revision prompt tells the model to trust them. On 2026-08-03, 122 of 130 strategies and
# 6 of the 11 running ones (including the live one) were blind in exactly this way.
#   'non-regression' (default) reject only if the candidate is blind AND the main.py it
#                    would be reverted to was not: a lineage may never go sighted->blind.
#   'strict'         reject any blind main.py. Note revert_main_py restores the parent's
#                    file, which in most lineages is itself blind, so strict mostly
#                    discards near-misses (a real decide() plus one stray top-level
#                    statement) in exchange for an equally blind parent. Worth revisiting
#                    once most of the population is sighted.
#   'off'            report the finding, gate nothing.
MAIN_PY_IMPORTABILITY = os.environ.get('MAIN_PY_IMPORTABILITY', 'non-regression')

# Odds that a freshly bootstrapped strategy which ended up with no assets is handed the
# next assets from the Reflector oracle's tracked list. A coin flip rather than always:
# the point is to introduce non-XLM legs into a population that otherwise cannot invent
# one, while keeping XLM-only seeds in the mix as the control group to rank them against.
REFLECTOR_INJECT_CHANCE = 0.5
REFLECTOR_INJECT_COUNT = 2

# The market recorder. It used to be one snapshot per cycle taken inline in run(), which
# was fine while nothing read the rows back. The DEX/CEX basis changed that: it is a
# per-tick input, and dex_price.get_orderbook is uncached, so every strategy measuring
# its own would be ~40 Horizon order-book GETs a minute. One supervised writer at 60s
# feeds all of them through /opt/trades/.market_history.jsonl instead. The daemon
# deliberately outlives a single cycle -- a per-cycle process would leave gaps exactly
# as long as a revision takes.
RECORDER_SCRIPT = Path('/opt/tools/market_recorder.py')
RECORDER_INTERVAL = 60
RECORDER_PID_FILE = TRADES_DIR / '.market_recorder.pid'
RECORDER_LOG = TRADES_DIR / 'market_recorder.log'

# Odds that a template spawn is seeded with a `basis_min_bp` gate, and the percentile of
# the recorded tradeable_bp distribution used as its threshold. A coin flip for the same
# reason REFLECTOR_INJECT_CHANCE is one: the un-seeded spawns are the control arm that
# basis_report.py compares the seeded ones against. See _inject_basis_gate.
BASIS_INJECT_CHANCE = 0.5
BASIS_INJECT_PERCENTILE = 0.25
BASIS_MIN_RECORDED_HOURS = 6

# What one cycle adds to the population: two clones of the best distinct performers, plus
# one strategy pulled fresh from template_repo. The template spawn exists because every
# other newcomer descends from an existing strategy -- without it the population can only
# ever narrow around whatever the current leaders already do, and bootstrap_initial_strategies
# (the only other template path) fires just on first boot or a below-KEEP_TOP_N backfill.
# The cull is rank-based (see the performances[KEEP_TOP_N:] loop in run()), so adding three
# per cycle is what makes it stop three per cycle; there is no separate "how many to stop".
CLONES_PER_CYCLE = 2
TEMPLATE_SPAWNS_PER_CYCLE = 1

# Cap on revise-strategy subprocess calls per cycle, and the odds a cycle spends them at
# all. Every newcomer this cycle gets a revision. At 1 -- with the target drawn by a
# uniform random.choice over a batch that is two clones to one template spawn -- the
# fresh spawn, which is the only channel that can introduce logic the population does not
# already run, won the draw on 0.75 * 1/3 = 1/4 of cycles; every other newcomer inherited
# a parent's main.py verbatim, since apply_random_tweak only scales thresholds. Measured
# result: 141 main.py files across the population, ~10 distinct hashes, the top three
# covering 113 of them.
#
# Note this is deliberately NOT paired with a lower REVISION_TIMEOUT (still 60000s,
# ~16.7h): three sequential revisions may legally run far past CYCLE_SLEEP and outlast an
# emperor.sh window, which is the risk the cap of 1 originally existed to avoid. That is
# an accepted trade, and it only binds if the model hangs -- REVISION_TIMEOUT reads from
# the environment, so it can be lowered without a code edit if cycles run long.
#
# The ~25% of cycles that revise nothing are not a failure mode -- every newcomer still
# gets apply_random_tweak or apply_seed_thresholds, the same exploration the fallback
# path has always done.
REVISIONS_PER_CYCLE = 3
REVISION_CHANCE = 0.75

# The two jobs a newcomer can be handed. `refine` is a clone of a leader: improve on a
# parent with a real record. `explore` is a fresh template spawn with no meaningful
# parent, asked for something structurally different from the incumbents. The role picks
# which user prompt master-agent.py builds, and is passed to it as the 6th argv of
# `revise-strategy`. Duplicated there (its filename has a hyphen, so it cannot be
# imported); keep the two pairs in sync.
ROLE_REFINE = 'refine'
ROLE_EXPLORE = 'explore'

PRICE_FETCH_ATTEMPTS = 3
PRICE_RETRY_DELAY = 60      # seconds between price-fetch attempts
PRICE_FAILURE_SLEEP = 300   # seconds to wait before retrying a cycle that got no price
CYCLE_SLEEP = 3600

# Cooperative shutdown request. Touch this file and monitor.py exits the next time it
# reaches a cycle-boundary sleep instead of sleeping. This is how emperor.sh ends a
# monitor window now: a SIGTERM to the process group can land anywhere, including in the
# middle of an in-flight revise-strategy subprocess, a _sanitize_assets rewrite of a
# clone's config.json, or a commit_revision git commit -- leaving a half-revised clone
# that the next cycle then has to prune. Checked only at the sleeps, so by construction
# nothing is in flight when it fires. emperor.sh still falls back to TERM if this is
# ignored (the wait there has to exceed CYCLE_SLEEP, since a monitor that is already
# asleep will not notice the file until the sleep ends).
EXIT_FILE = Path('/opt/.monitor.py.exit')

def sleep_or_exit(seconds, what='cycle'):
    """Sleep `seconds`, or exit 0 now if a shutdown has been requested.

    Removes EXIT_FILE on the way out so the next monitor.py run isn't stopped
    immediately by a stale request (emperor.sh also clears it, for the case where
    monitor.py died before it could).
    """
    if EXIT_FILE.exists():
        print(f'{EXIT_FILE} exists; exiting instead of sleeping {seconds}s before next {what}')
        try:
            EXIT_FILE.unlink()
        except OSError as e:
            print(f'Warning: could not remove {EXIT_FILE}: {e}')
        sys.stdout.flush()
        raise SystemExit(0)
    print(f'Sleeping for {seconds}s...')
    time.sleep(seconds)

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

def turnover_stats(name):
    """(trades, turnover_usd, friction_usd) from this strategy's trade log.

    Turnover is the sum of every fill's notional -- the quantity a spread is charged on,
    and the one number that says whether a strategy has an edge or just a habit. On
    2026-08-03 the leader had turned over $5,757 to gain $23.33: a 40.5 bp edge, against
    a book that costs ~8.7 bp round trip and extra-asset books that cost 150-190. That
    ratio existed in the trade logs all along and nothing ever computed it, so "962
    trades" read as evidence of a working strategy rather than as a cost to be covered.

    `friction_usd` is summed from the per-line friction_bp that trade_logger started
    writing on 2026-08-03. Lines from before that are counted into turnover but
    contribute no cost, which is correct -- they genuinely were not charged any.
    """
    log_path = trade_log_path(name)
    if not log_path.exists():
        return 0, 0.0, 0.0
    trades = 0
    turnover = friction_usd = 0.0
    try:
        with log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                trades += 1
                notional = abs(float(row.get('amount_usd') or 0.0))
                turnover += notional
                bp = row.get('friction_bp')
                if bp:
                    friction_usd += notional * float(bp) / 10000.0
    except Exception as e:
        print(f'Could not read trade log for {name}: {e}')
    return trades, turnover, friction_usd


def print_turnover_report(performances, limit=KEEP_TOP_N):
    """One block per cycle: what the leaders traded, and what trading it cost them.

    Sits next to the score table deliberately. Score says who is winning; this says
    whether they are winning by having an edge or by being lucky with a lot of coin
    flips, which is the distinction the cull could not previously make.
    """
    rows = []
    for name, score in performances[:limit]:
        trades, turnover, friction_usd = turnover_stats(name)
        if not trades:
            continue
        gain = score - MIN_LIVE_SCORE      # vs the 1000.00 starting balance
        # Edge per dollar traded. Below the round-trip cost of the book it trades on,
        # a strategy is paying to play.
        edge_bp = (gain / turnover * 10000) if turnover > 0 else None
        rows.append((name, trades, turnover, gain, edge_bp, friction_usd))
    if not rows:
        return
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import friction
        cost_bp = friction.round_trip_bp('XLM')
    except Exception:
        cost_bp = None
    header = f'Turnover vs edge (XLM round trip costs {cost_bp} bp):' if cost_bp \
        else 'Turnover vs edge:'
    print(header)
    for name, trades, turnover, gain, edge_bp, friction_usd in rows:
        edge = f'{edge_bp:.1f} bp/$' if edge_bp is not None else 'n/a'
        # The verdict, spelled out: an edge thinner than the toll is not an edge.
        verdict = ''
        if edge_bp is not None and cost_bp:
            verdict = '  <-- edge below trading cost' if edge_bp < cost_bp else ''
        print(f'  {name}: {trades} trades, ${turnover:,.0f} turnover, '
              f'{gain:+.2f} gain, {edge}, ${friction_usd:.2f} paid{verdict}')


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


def _stellar_caps():
    """stellar_trader's current caps, or None. Never raises.

    Lazy and defensive for the same reasons _tools() is. Only ever used to *record* what
    the caps were when a strategy was promoted -- a strategy earns the live flag on a
    paper track record sized by its own config.json, then trades real money clamped by
    these, and the two numbers are not the same. Failing to read them must degrade that
    record, never block a promotion.

    The non-base caps are included because they now bind: a non-XLM leg trades real money
    and is clamped to MAX_TRADE_USD_NONBASE, an eighth of the XLM per-trade cap. Recording
    only the XLM caps made every extra leg's sizing look 8x larger than it can be, and
    left live_report unable to see drift in the caps that actually govern it.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import stellar_trader
        return {'max_trade_usd': float(stellar_trader.MAX_TRADE_USD),
                'max_daily_usd': float(stellar_trader.MAX_DAILY_USD),
                'max_trade_usd_nonbase': float(stellar_trader.MAX_TRADE_USD_NONBASE),
                'max_daily_usd_per_asset': float(stellar_trader.MAX_DAILY_USD_PER_ASSET),
                'max_position_usd_per_asset': float(stellar_trader.MAX_POSITION_USD_PER_ASSET),
                'max_total_nonbase_exposure_usd': float(
                    stellar_trader.MAX_TOTAL_NONBASE_EXPOSURE_USD),
                'max_stuck_usd': float(stellar_trader.MAX_STUCK_USD)}
    except Exception as e:
        print(f'could not read stellar_trader caps ({e}); recording sizing without them')
        return None


def _importability_check(source_or_path):
    """(ok, reason) from backtest.importability_report, or None if it can't be consulted.

    None means "cannot tell", and every caller FAILS OPEN on it. That is deliberately the
    opposite of main_py_calls_execute_trade, which fails closed: that one guards real
    money, this one guards a *fitness signal*. Failing closed whenever /opt/tools were
    missing or mid-rewrite would revert every revised main.py in the population, freeze
    main.py evolution entirely and route every clone to apply_random_tweak -- a much
    larger and much quieter failure than the blindness it is trying to prevent, and one
    that would read in the logs as a run of bad revisions.

    Also tolerates a /opt/tools that predates importability_report: emperor.sh rewrites
    the two repos independently, so version skew between them is a real state, not a
    hypothetical.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import backtest
        report = getattr(backtest, 'importability_report', None)
        if report is not None:
            return report(source_or_path)
        ok = backtest._is_importable(source_or_path)
        return ok, ('decide() is importable' if ok
                    else 'backtest cannot import a top-level decide() from main.py')
    except Exception as e:
        print(f'importability check unavailable ({e}); not gating on it')
        return None


def _recorder_alive():
    """Is the market recorder daemon running? Pid file plus two confirmations.

    os.kill(pid, 0) alone is not enough. This pid file survives a container restart,
    and pids are recycled, so a stale file can name a live and entirely unrelated
    process -- at which point monitor would believe it had a recorder forever and every
    basis reader would silently degrade to neutral. /proc/<pid>/cmdline settles it.
    """
    try:
        pid = int(RECORDER_PID_FILE.read_text().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    try:
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes().decode('utf-8', 'replace')
    except Exception:
        return False    # cannot confirm -> assume not ours and respawn
    return 'market_recorder' in cmdline


def ensure_market_recorder():
    """Start the market recorder daemon if it is not already running, then report.

    Idempotent, and called once per cycle: the daemon outlives a cycle (and an emperor
    window, since it is setsid'd out of monitor's process group), so the common path
    here is the _recorder_alive() check and nothing else.

    _strategy_python(), never a bare python3. /usr/bin/python3 cannot import the
    third-party packages in /opt/agents/venv, and a bare python3 is exactly what made
    every revision fail silently for weeks -- the same trap, one directory over.
    """
    if not _recorder_alive():
        TRADES_DIR.mkdir(parents=True, exist_ok=True)
        log = open(RECORDER_LOG, 'a')
        proc = subprocess.Popen(
            [_strategy_python(), '-u', str(RECORDER_SCRIPT),
             '--daemon', '--interval', str(RECORDER_INTERVAL)],
            stdout=log, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,   # survives a TERM to monitor's process group
        )
        RECORDER_PID_FILE.write_text(str(proc.pid))
        print(f'Started market recorder (pid {proc.pid}, every {RECORDER_INTERVAL}s) '
              f'-> {RECORDER_LOG}')

    if '/opt/tools' not in sys.path:
        sys.path.append('/opt/tools')
    import market_recorder
    span = market_recorder.span()
    # A cycle in which the recorder was just started legitimately has no fresh row yet;
    # one in which it has been up for hours and still has none is the failure that would
    # otherwise be invisible, because every basis reader degrades to neutral in silence.
    row = market_recorder.tail(1)
    last = row[0] if row else {}
    age = round(time.time() - last['ts']) if last.get('ts') else None
    print(f"Market history: {span['rows']} rows over {span['hours']}h; "
          f"last row {age}s ago, basis {last.get('basis_bp')} bp, "
          f"tradeable {last.get('tradeable_bp')} bp, spread {last.get('spread_bp')} bp")
    if age is not None and age > RECORDER_INTERVAL * 5:
        print(f'WARNING: market history is {age}s stale; every basis reader is '
              f'degrading to neutral. Check {RECORDER_LOG}')


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

    # One order book per asset per cycle, carrying the bid ladder so scoring can
    # depth-cap each strategy's position without any further network calls.
    for spec in sorted(specs):
        mark = dex_price.get_mark_with_depth(spec)
        if mark and mark.get('price'):
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


def _normalize_config(cfg_path, name):
    """Fill the three required keys monitor always knows the right value for.

    _required_keys_are_sane demands name, schema_version and trade_amount_usd. On
    2026-08-04, 101 of 141 strategy configs had no schema_version at all -- it predates
    them -- so requiring it without this would reject most revised clones over a key
    their parent never had, discard the model's work, and fall through to a random
    tweak. Rejecting is only the right answer when monitor cannot know the correct
    value; for these three it always can, so it supplies them instead.

    ABSENT KEYS ONLY. This deliberately does not correct a key that is present but
    wrong. A `name` naming some other strategy means the model copied a whole
    config.json from somewhere -- most likely the parent's, which the refine prompt
    hands it verbatim -- and that is a revision to reject, not to patch: whatever else
    it copied is suspect too. Overwriting it here would make the name check dead code.

    Returns True if it rewrote the file.
    """
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return False        # unparseable; _config_is_sane will fail it and the
                            # fallback rebuilds the file from scratch
    if not isinstance(cfg, dict):
        return False

    filled = {}
    if 'name' not in cfg:
        filled['name'] = name
    if 'schema_version' not in cfg:
        filled['schema_version'] = 2
    if 'trade_amount_usd' not in cfg:
        filled['trade_amount_usd'] = 10.0
    if not filled:
        return False

    cfg.update(filled)
    try:
        json.dump(cfg, open(cfg_path, 'w'), indent=2)
    except Exception as e:
        print(f'  could not normalize config for {name}: {e}')
        return False
    print(f'  filled missing config keys for {name}: '
          f'{", ".join(f"{k}={v!r}" for k, v in filled.items())}')
    return True


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


# Cursor over the Reflector oracle's tracked-asset list, used to hand freshly
# bootstrapped strategies something to trade besides XLM. In memory only: losing it on
# restart just means starting the walk over, and persisting it would be one more file to
# keep consistent for no benefit.
#
# This exists because nothing else in monitor can introduce an asset. _sanitize_assets
# only removes, apply_seed_thresholds only clears, and apply_random_tweak only copies the
# parent's -- so with the revision model unresponsive, every clone falls through to the
# tweak fallback and the population can never discover a non-XLM leg on its own.
_reflector_pool = {'assets': [], 'index': 0}

# Codes that are never worth injecting as a *tradeable extra leg*, whatever their
# liquidity. USDC and the other dollar anchors are the quote asset: "buying" one with USD
# at ~$1.00 opens a position that cannot appreciate, consumes one of the two
# MAX_EXTRA_ASSETS slots, and pays the spread twice for the privilege. They rank near the
# top of discover_candidates precisely because they are the most liquid things on the
# network, so without this the liquidity-first pool below would hand out USDC first,
# every time.
_UNTRADEABLE_CODES = {'USDC', 'USDT', 'USD', 'DAI', 'BUSD', 'TUSD', 'USDX', 'YUSDC'}

# An injected leg's threshold band must be at least this many times its round-trip
# trading cost, or the band is mostly toll. See _inject_discovered_assets.
BAND_COST_MULTIPLE = 4.0


def _leg_round_trip(spec):
    """Round-trip cost for `spec` as a fraction, or 0.0 if friction can't be consulted.

    Fails open, unlike friction.py's own default: this only widens a threshold band, and
    a tools outage should leave the band at the historical +/-2% rather than silently
    widening every injected leg to the non-base floor.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import friction
        return friction.half_spread(spec) * 2
    except Exception:
        return 0.0

# Same cursor shape as _reflector_pool, over asset_discovery.discover_candidates() --
# stellar.expert's liquidity/rating-ranked universe. This is the primary source now; see
# _next_candidate_assets.
_discovered_pool = {'assets': [], 'index': 0}


def _next_discovered_assets(count=2):
    """The next `count` liquidity-ranked candidates, cycling and refreshing when spent.

    Why this exists alongside the Reflector pool: on 2026-08-03 the Reflector channel had
    a 0% admission rate. Every single asset it proposed was rejected by _sanitize_assets
    moments later -- apUSDT (113 trustlines, needs 200), asUSDC (80.7% spread), KES (200%
    spread), VEUR (no mark at all). That is not bad luck. reflector_oracle tracks a price
    feed, and most of what it feeds prices for is fiat forex anchors that barely trade on
    the Stellar DEX, so the one mechanism that can introduce a non-XLM leg into the
    population was structurally incapable of introducing one.

    asset_discovery.discover_candidates() was already here, already ranked by
    stellar.expert's composite rating and trustline count, and returned exactly the
    assets that DO pass -- AQUA, XRP, BTCLN, ZARZ. It was reachable only from the
    revision LLM's tool surface, which was itself dead (see _check_revision_interpreter),
    so nothing mechanical had ever used it.

    Same contract as _next_reflector_assets: [] on failure, shuffled on refresh so a
    restart doesn't re-propose the alphabetical/ranked head forever, and every pick is a
    PROPOSAL that _sanitize_assets re-verifies independently.
    """
    pool = _discovered_pool
    if pool['index'] >= len(pool['assets']):
        try:
            if '/opt/tools' not in sys.path:
                sys.path.append('/opt/tools')
            import asset_discovery
            found = asset_discovery.discover_candidates(limit=50) or []
        except Exception as e:
            print(f'  could not refresh the discovered asset pool: {e}')
            found = []
        pool['assets'] = [a for a in found
                          if (a.get('code') or '').upper() not in _UNTRADEABLE_CODES]
        random.shuffle(pool['assets'])
        pool['index'] = 0
        if not pool['assets']:
            return []
        print(f"  refreshed discovered asset pool: {len(pool['assets'])} ranked assets "
              f"(shuffled; first few: {', '.join(a['code'] for a in pool['assets'][:4])})")

    picks = pool['assets'][pool['index']:pool['index'] + count]
    pool['index'] += len(picks)
    return picks


def _next_candidate_assets(count=2):
    """Assets to propose to a fresh strategy: liquidity-ranked first, oracle as backup.

    Kept as two separate pools rather than one merged list so the fallback is legible in
    the logs: if discover_candidates is down (stellar.expert unreachable), the cycle
    still proposes *something* rather than proposing nothing, and the log line says which
    source it came from.
    """
    picks = _next_discovered_assets(count)
    if picks:
        return picks
    print('  discovered pool empty; falling back to the Reflector oracle list')
    return _next_reflector_assets(count)


def _next_reflector_assets(count=2):
    """The next `count` oracle-tracked assets, cycling and refreshing when exhausted.

    Returns [] if the oracle is unreachable, so injection simply doesn't happen that
    round -- the same fail-safe degradation to XLM-only the rest of the asset stack uses.

    A short tail (one left when two are wanted) yields just that one rather than
    refreshing mid-call to top up a single slot: a refresh costs 1-2 minutes of CLI
    invocations, and the caller is happy with fewer candidates.

    The pool is shuffled on every refresh. reflector_oracle.get_tracked_assets() returns
    its assets sorted by spec, and this walks them in order, so an unshuffled pool hands
    out the alphabetical head first every single time the process restarts -- AQUA and ARS
    to whoever injects first, and the tail end of the alphabet only to a monitor that
    stays up long enough to walk ~38 assets at two per injection. Restarts are frequent
    (emperor.sh ends a window every EMPEROR_RUN_HOURS), so in practice the same handful of
    codes were being proposed over and over and most of the oracle's list never got tried.
    """
    pool = _reflector_pool
    if pool['index'] >= len(pool['assets']):
        try:
            if '/opt/tools' not in sys.path:
                sys.path.append('/opt/tools')
            import reflector_oracle
            pool['assets'] = reflector_oracle.get_tracked_assets()
        except Exception as e:
            print(f'  could not refresh the Reflector asset pool: {e}')
            pool['assets'] = []
        random.shuffle(pool['assets'])
        pool['index'] = 0
        if not pool['assets']:
            return []
        print(f"  refreshed Reflector asset pool: {len(pool['assets'])} tracked assets "
              f"(shuffled; first few: {', '.join(a['code'] for a in pool['assets'][:4])})")

    picks = pool['assets'][pool['index']:pool['index'] + count]
    pool['index'] += len(picks)
    return picks


def _inject_basis_gate(cfg_path):
    """Give a template spawn a coin flip at trading on the DEX/CEX basis.

    Returns True if config.json was written. Same shape and the same reasoning as
    _inject_discovered_assets below: the population cannot invent this on its own, since
    apply_random_tweak only scales existing thresholds and apply_seed_thresholds only
    rebuilds the price band, so without a mechanical channel `basis_min_bp` would enter
    the population only if the revision model happened to write it. A coin flip rather
    than always, because the un-gated spawns ARE the control arm -- basis_report.py's
    whole output is the comparison between the two, and seeding every spawn would leave
    nothing to compare against.

    The threshold is derived from the recorded distribution rather than being a literal,
    for a reason worth stating plainly: tradeable_bp is negative nearly all the time (the
    basis is typically a fraction of the spread -- see basis.py's calibration note), so a
    positive literal would produce a strategy that never buys, holds cash forever, scores
    a flat 1000.00, and is indistinguishable on the leaderboard from a broken one. The
    25th percentile of the recent distribution instead means "skip the buy when the venue
    is unusually bad", which is a gate that actually fires and can therefore be wrong --
    the only kind worth an experimental arm.

    Self-sequencing: refuses until there are BASIS_MIN_RECORDED_HOURS of history to draw
    that percentile from. No arm is created before there is data to feed it, so this
    needs no operator timing and no second deploy.
    """
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return False
    if cfg.get('basis_min_bp') is not None:
        return False    # already gated (inherited or model-written); leave it alone

    if random.random() >= BASIS_INJECT_CHANCE:
        return False

    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import market_recorder
        span = market_recorder.span()
        if span['hours'] < BASIS_MIN_RECORDED_HOURS:
            print(f"  no basis gate: only {span['hours']}h of recorded history "
                  f"(need {BASIS_MIN_RECORDED_HOURS}h)")
            return False
        # spec='XLM' explicitly: the gate is on the XLM leg, and the recorder can be
        # pointed at another spec, at which point an unfiltered series would mix two
        # assets' dislocations into one percentile.
        values = sorted(v for v in market_recorder.series('tradeable_bp', hours=72,
                                                          spec='XLM')
                        if v is not None)
        if len(values) < 30:
            print(f'  no basis gate: only {len(values)} tradeable_bp readings')
            return False
    except Exception as e:
        print(f'  no basis gate: recorded history unavailable ({e})')
        return False

    threshold = round(values[int(len(values) * BASIS_INJECT_PERCENTILE)], 2)
    cfg['basis_min_bp'] = threshold
    try:
        json.dump(cfg, open(cfg_path, 'w'), indent=2)
    except Exception as e:
        print(f'  could not write basis gate ({e})')
        return False
    print(f'  basis gate: basis_min_bp={threshold} '
          f'(p{int(BASIS_INJECT_PERCENTILE * 100)} of {len(values)} readings)')
    return True


def _inject_discovered_assets(cfg_path, marks=None, count=2):
    """Give a still-XLM-only bootstrapped strategy a coin flip at two discovered assets.

    Returns True if config.json was written. The picks are *proposals*, exactly like an
    LLM-chosen asset: the caller runs _sanitize_assets straight afterwards, so every
    normal verification rule (clawback, auth_required, trustlines, age, spread, bid
    depth, pinned issuers) still decides what actually survives. "Next two" therefore
    routinely yields one or zero.

    Any pick with no mark is skipped -- a leg with no price cannot be given sane
    thresholds, would never trade, and would fail _assets_are_sane if a mark appeared
    later. Marks that are fetched get folded into `marks` so the cycle's sanity checks
    validate the new legs rather than skipping them as unpriced.
    """
    tools = _tools()
    if tools is None:
        return False
    portfolio, dex_price, _ = tools

    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return False
    if portfolio.assets_from_config(cfg):
        return False  # already has assets; nothing to seed

    if random.random() >= REFLECTOR_INJECT_CHANCE:
        return False

    candidates = _next_candidate_assets(count)
    if not candidates:
        return False

    base_size = float(cfg.get('trade_amount_usd') or 10.0)
    injected = []
    for candidate in candidates:
        mark = dex_price.get_mark(candidate['spec'])
        if not mark or mark <= 0:
            print(f"  skipping {candidate['spec']}: no mark")
            continue
        # The band starts at the +/-2% apply_seed_thresholds gives the XLM leg, but is
        # widened if this asset's own book is wide enough to eat it. A round trip inside
        # the band earns (band - round_trip_cost); on the XLM book (8.7 bp) a 4% band
        # keeps essentially all of it, but AQUA's book measured 151 bp round trip and
        # ARS's 186 bp, so a 4% band on those hands back a third to a half of every
        # winning trade before anything else happens. BAND_COST_MULTIPLE keeps the round
        # trip at most 1/N of the band, which on a thin asset simply means it trades
        # less often and only on moves actually worth crossing for.
        # Rounded to 9dp to match apply_random_tweak's per-leg precision -- which is why
        # the result has to be re-checked: several tracked assets mark below 1e-3, and
        # one below ~5e-10 would round its band flat to 0.0 (or to buy == sell), failing
        # _assets_are_sane and dragging the whole config into the fallback.
        half_band = max(0.02, _leg_round_trip(candidate['spec']) * BAND_COST_MULTIPLE / 2)
        buy_below = round(mark * (1 - half_band), 9)
        sell_above = round(mark * (1 + half_band), 9)
        if buy_below <= 0 or buy_below >= sell_above:
            print(f"  skipping {candidate['spec']}: mark {mark!r} is too small to give "
                  f"a representable threshold band")
            continue
        injected.append({
            'code': candidate['code'],
            'issuer': candidate['issuer'],
            'buy_below': buy_below,
            'sell_above': sell_above,
            # Deliberately smaller than the XLM leg: these books are thin, which is
            # what the revision prompt tells the model about extra assets too.
            'trade_amount_usd': round(base_size / 4, 2),
        })
        if marks is not None:
            marks[candidate['spec']] = mark

    if not injected:
        return False

    cfg['assets'] = injected
    try:
        json.dump(cfg, open(cfg_path, 'w'), indent=2)
    except Exception as e:
        print(f'  could not write injected assets: {e}')
        return False
    print(f"  seeded {len(injected)} discovered asset(s): "
          f"{', '.join(a['code'] for a in injected)}")
    return True


# The old name, kept because it is referenced in CLAUDE.md, default-assets.md and several
# emperor_logs, and a future pass reading those should not find a NameError.
_inject_reflector_assets = _inject_discovered_assets


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
        mark = portfolio.mark_price((marks or {}).get(asset['spec']))
        if not mark:
            continue       # unpriceable right now; _sanitize_assets handles it
        buy, sell = asset['buy_below'], asset['sell_above']
        if buy <= 0 or sell <= 0 or buy >= sell:
            return False
        if buy < mark * 0.5 or sell > mark * 1.5:
            return False
    return True


def _required_keys_are_sane(cfg, name=None):
    """The keys every config must carry, beyond the thresholds.

    These went unchecked for a long time because they look like bookkeeping. `name` is
    not: trade_logger writes to /opt/trades/<config name>.log, and that log is what
    score.py and qualifies_for_live() read. A clone whose config says someone else's
    name logs its trades under that name -- so it looks like it never trades, while
    inflating the trade count of a strategy that might be promoted to real money on it.
    That is the specific failure `name` is passed in to catch, and it is a plausible one:
    the refine prompt hands the model the parent's entire config.json, so copying it
    wholesale (name included) is one lazy write_file away.

    schema_version is accepted as any positive int rather than pinned to 2. Nothing in
    the system actually reads config's schema_version -- portfolio.SCHEMA_VERSION is
    state.json's, a different thing -- so pinning it would reject a config over a fact
    no consumer has an opinion about.

    Missing keys are normally filled by _normalize_config before this runs; what is left
    for this to catch is present-but-wrong, which monitor cannot safely guess a fix for.
    """
    cfg_name = cfg.get('name')
    if not isinstance(cfg_name, str) or not cfg_name.strip():
        return False
    if name is not None and cfg_name != name:
        return False
    try:
        if int(cfg['schema_version']) <= 0:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    try:
        if float(cfg['trade_amount_usd']) <= 0:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _thresholds_are_sane(cfg, price):
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
    return True


def _config_is_sane(cfg, price, marks=None, name=None):
    # Split into three so the smoke-test prep in main_py_is_sane can ask the narrower
    # question it actually means (are the thresholds usable?) without a missing
    # schema_version causing it to overwrite thresholds the model deliberately chose.
    return (_required_keys_are_sane(cfg, name)
            and _thresholds_are_sane(cfg, price)
            and _assets_are_sane(cfg, marks))

def main_py_is_sane(strategy_dir, name, price, marks=None, *, baseline_source=None):
    """Check that a revised main.py actually parses, runs, and persists state.

    _config_is_sane only ever looked at config.json, so a revision that gutted
    main.py sailed straight through and got started. On 2026-08-01 seed_0_1785619070
    was handed to a model that wrote main.py eight times, each attempt a syntax
    error, shrinking 3892 -> 377 bytes until the file finally *parsed* -- a stub
    with no trading loop that exits immediately and never writes state.json. It
    scored -inf (0 trades) forever after, and the config-only gate never noticed.

    Returns (ok, reason).

    `baseline_source` is the main.py this candidate would be reverted to (see
    _main_py_at). It is only consulted by the importability gate below, which refuses a
    revision that would take a lineage from backtest-visible to backtest-blind.

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

    # Before the smoke run, deliberately: this is a pure AST walk costing microseconds,
    # the smoke run costs SMOKE_TEST_SECONDS (120s) per candidate, and "it also runs"
    # adds nothing once the verdict is decided. The reason string names the offending
    # line, because "not importable" on its own is unactionable to the next revision.
    if MAIN_PY_IMPORTABILITY != 'off':
        verdict = _importability_check(source)
        if verdict is not None and not verdict[0]:
            why = verdict[1]
            baseline_ok = None
            if baseline_source is not None:
                baseline_verdict = _importability_check(baseline_source)
                baseline_ok = baseline_verdict[0] if baseline_verdict else None
            if MAIN_PY_IMPORTABILITY == 'strict' or baseline_ok:
                return False, (
                    f'backtest cannot import decide() from main.py ({why}), so '
                    f'beats_buy_hold would describe config.json thresholds rather than '
                    f'this code' + (' -- and the main.py it replaces was importable'
                                    if baseline_ok else ''))
            # Both blind: rejecting would revert to an equally blind parent, throwing
            # away whatever else the revision did to main.py for no gain in visibility.
            # Recorded so the emperor pass can see it; not gated. See
            # MAIN_PY_IMPORTABILITY.
            print(f'NOTE: {name} is backtest-blind ({why}); the main.py it replaces was '
                  f'too, so this is not treated as a regression')

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
        if not _thresholds_are_sane(cfg, price):
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
        # Not 'python3' -- the same bug as _check_revision_interpreter. This must be the
        # interpreter strat_manager will really start this main.py under, which is why it
        # calls that resolution rather than reimplementing it or just using sys.executable:
        # otherwise the smoke test measures a different environment than the one that runs.
        # A revision importing a package the venv has and /usr/bin/python3 does not would
        # pass here and die on its first real tick, or be reverted for an import that would
        # in fact have resolved -- and either way the gate would be reporting on code other
        # than the code that trades.
        proc = subprocess.Popen([_strategy_python(), '-u', 'main.py'], cwd=str(tmp_dir),
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

def _main_py_at(strategy_dir, before_head):
    """main.py's contents at `before_head` -- exactly what revert_main_py would restore.

    Read out of git rather than off disk, so it is unaffected by the model rewriting the
    working tree between the check and the revert. None if it can't be read.
    """
    if not before_head:
        return None
    r = _git(strategy_dir, 'show', f'{before_head}:main.py')
    return r.stdout if r.returncode == 0 else None

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
    existing = {}
    if cfg_path.exists():
        try:
            loaded = json.load(open(cfg_path))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            pass
    trade_amount_usd = existing.get('trade_amount_usd', 10.0)
    existing_assets = existing.get('assets') or []
    # Start from whatever is already in the file rather than a fresh literal -- see the
    # same change in apply_random_tweak for why. A template spawn reaching this fallback
    # has usually had a revision fail a gate, and any config knob that revision invented
    # is still in the file; there is no reason for the threshold repair to take it out.
    new_cfg_data = dict(existing)
    new_cfg_data.update({
        'name': name,
        'schema_version': existing.get('schema_version', 2),
        'buy_below': round(price * 0.98, 6),
        'sell_above': round(price * 1.02, 6),
        'trade_amount_usd': trade_amount_usd,
        # Carried across explicitly rather than left to the dict copy above, because it
        # used to hard-write [] -- which was fine while seeds were always XLM-only, but
        # this runs in the `not revised` branch, exactly the case
        # _inject_discovered_assets targets, so clearing it here would wipe every
        # injected leg moments after it was written. Only the XLM thresholds are seeded;
        # the extra legs already carry their own, derived from their marks.
        'assets': existing_assets if isinstance(existing_assets, list) else [],
    })
    json.dump(new_cfg_data, open(cfg_path, 'w'), indent=2)

def _revision_budget():
    """How many revise-strategy calls this cycle may make: REVISIONS_PER_CYCLE or 0.

    Rolled once per cycle, deliberately -- not once per candidate. The budget is shared by
    every path that can create a strategy (the clone loop and bootstrap_initial_strategies
    alike), so the REVISIONS_PER_CYCLE ceiling holds even on a backfill cycle that seeds
    eight strategies at once. That used to be eight back-to-back REVISION_TIMEOUT waits.

    All-or-nothing, not a per-candidate coin flip: the ~25% of cycles that return 0 are
    the control arm, a whole batch built by apply_random_tweak / apply_seed_thresholds
    alone, which is what the revised batches are compared against.
    """
    return REVISIONS_PER_CYCLE if random.random() < REVISION_CHANCE else 0

_REVISION_INTERPRETER_CHECKED = []


def _check_revision_interpreter():
    """Warn loudly, once per process, if sys.executable cannot import ollama.

    On 2026-08-03 every revision in every observed cycle had been failing with
    `ModuleNotFoundError: No module named 'ollama'`, and the only trace was one line of
    captured stderr buried in the monitor log. The cause was this function's subprocess
    spawning a bare `python3`: only /opt/agents/venv/bin/python has the ollama package,
    and /usr/bin/python3 -- what PATH resolves to under `docker exec`, cron, or
    emperor.sh's setsid, i.e. every non-interactive launch -- does not. So the entire
    LLM layer was dead, monitor fell through to apply_random_tweak on every clone, and
    the system's "evolution" was random threshold jitter with no model in the loop. From
    the outside it looked like a run of unremarkable cycles.

    The spawn below now uses sys.executable, which by construction is whatever monitor
    itself is running under. This check exists for the remaining case: monitor started
    under an interpreter that has no ollama either. That is still a total failure of
    revision, but it will now say so at the top of the cycle instead of being inferred
    from a fallback message that looks identical to a healthy quota-exhausted cycle.
    """
    if _REVISION_INTERPRETER_CHECKED:
        return _REVISION_INTERPRETER_CHECKED[0]
    ok = False
    try:
        probe = subprocess.run([sys.executable, '-c', 'import ollama'],
                               capture_output=True, text=True, timeout=60)
        ok = probe.returncode == 0
        if not ok:
            why = ((probe.stderr or '').strip().splitlines() or ['?'])[-1]
            print(f'WARNING: {sys.executable} cannot import ollama ({why}) -- '
                  f'EVERY revision this cycle will fail and fall back to a random tweak. '
                  f'Start monitor.py under an interpreter that has it '
                  f'(e.g. /opt/agents/venv/bin/python).')
    except Exception as e:
        print(f'WARNING: could not probe the revision interpreter ({e})')
    _REVISION_INTERPRETER_CHECKED.append(ok)
    return ok


def _run_revision(name, parent_name, score, leaderboard, price, role=ROLE_REFINE):
    """Hand one strategy to `master-agent.py revise-strategy`. True only on a clean exit.

    The single call site for the revision subprocess. master-agent.py already turns a
    model error into a non-zero exit (a reply starting with '[error:'), so a False here
    means the caller must fall back to a mechanical tweak rather than start a clone that
    is byte-identical to its parent.

    `role` selects which user prompt master-agent.py builds -- see ROLE_REFINE /
    ROLE_EXPLORE. Passed as a trailing positional because that end of master-agent.py is
    a bare `revise_strategy(*sys.argv[2:])` splat with defaults.
    """
    if not MASTER_AGENT_SCRIPT.exists():
        return False
    _check_revision_interpreter()
    try:
        result = subprocess.run(
            # sys.executable, NOT 'python3' -- see _check_revision_interpreter. The
            # revision subprocess must run under the same interpreter monitor does, or it
            # silently loses access to every dependency monitor was started with.
            [sys.executable, str(MASTER_AGENT_SCRIPT), 'revise-strategy',
             name, parent_name, str(score), leaderboard, str(price), role],
            capture_output=True, text=True, timeout=REVISION_TIMEOUT,
        )
        if result.returncode == 0:
            # Names the role: emperor_logs are the primary input to the next emperor
            # pass, and "did explore ever produce a top-8 strategy" is otherwise
            # unanswerable from the log alone.
            print(f'Master agent revised {name} ({role}):\n{result.stdout.strip()}')
            return True
        print(f'Master agent revision failed for {name} ({role}): {result.stderr.strip()}')
    except Exception as e:
        print(f'Master agent revision errored for {name} ({role}): {e}')
    return False

def provision_strategy(name, source_repo, *, parent_name, parent_cfg, score, leaderboard,
                       price, marks, revise, inject, seed_fallback, role=None):
    """Create one strategy: clone -> optional revision -> gates -> fallback -> commit.

    The one path every new strategy takes, whether it is a clone of a winner or a fresh
    pull from template_repo. Only four things differ between those two cases, and they
    are the keyword arguments: `inject` (offer it Reflector assets), `seed_fallback`
    (rebuild config.json from the current price instead of nudging a parent's), `role`
    (which prompt the revision model is given), and `revise` (spend one of the cycle's
    revision slots on this one).

    Deliberately does not start the strategy -- callers do, because the clone loop starts
    its whole batch only after every clone has been gated and committed.
    """
    # Defaulted rather than required so a caller that forgets it still gets the right
    # prompt: parent_name == name is exactly the template-spawn case, and it is the one
    # the refine prompt degenerates on ("a new clone `seed_x` of `seed_x`", over an empty
    # state.json and a config whose buy_below == sell_above == 0.0).
    if role not in (ROLE_REFINE, ROLE_EXPLORE):
        role = ROLE_EXPLORE if parent_name == name else ROLE_REFINE
    # Names the parent explicitly: emperor_logs are the primary input to the next emperor
    # pass, and a clone's lineage is otherwise unrecoverable from the log alone.
    lineage = 'template' if parent_name == name else f'parent {parent_name}'
    print(f'Provisioning {name} from {lineage} ({source_repo}), role {role}, '
          f'{"revising" if revise else "no revision"}')
    subprocess.run(['/opt/strat_manager.py', 'clone', name, source_repo])
    strategy_dir = STRATEGIES_DIR / name
    cfg_path = strategy_dir / 'config.json'
    before_head = _git_head(strategy_dir)

    revised = _run_revision(name, parent_name, score, leaderboard, price, role) if revise else False

    touched = revision_changed_anything(strategy_dir, before_head)
    if revised and not touched:
        print(f'Revision for {name} left the clone identical to {parent_name}; treating as unrevised')
        revised = False

    # The one place a non-XLM leg can enter the population mechanically rather than by the
    # revision model choosing to write one -- _sanitize_assets only ever removes, and
    # apply_random_tweak only copies the parent's forward. Before sanitizing, so the picks
    # go through the exact same verification gate an LLM-chosen asset does.
    injected = False
    if inject and cfg_path.exists():
        injected = _inject_discovered_assets(cfg_path, marks, REFLECTOR_INJECT_COUNT)
        # The other mechanical channel, and the same `inject` (template-derived) gate:
        # clones inherit their parent's knob or absence of one, which is what makes a
        # lineage stay in the arm it was born into. `or injected` order matters only in
        # that both must run -- a spawn can legitimately get assets and a basis gate.
        injected = _inject_basis_gate(cfg_path) or injected

    # Supply any of the three required keys the config is missing before the gate asks
    # for them. Here rather than before the revision, deliberately: this writes
    # config.json, and `touched` above is a dirty-tree check -- normalizing any earlier
    # would mark every clone as touched and force the SMOKE_TEST_SECONDS smoke run on
    # clones nothing had actually changed. Same reason _sanitize_assets sits here.
    if cfg_path.exists():
        _normalize_config(cfg_path, name)

    # Always re-verify declared assets, revised or not: `assets` is committed to git and
    # inherited by every future clone of this lineage, apply_random_tweak copies it forward
    # verbatim, and an asset that was fine when admitted can be rugged later. Before the
    # smoke test, so the smoke run exercises the asset list the strategy will start with.
    if cfg_path.exists():
        _sanitize_assets(cfg_path, marks)

    # Runs whenever the model touched the repo, even if `revised` is already False: both
    # fallbacks only rewrite config.json, so a gutted main.py would otherwise survive a
    # failed config check and get started anyway (seed_0_1785619070).
    # `or injected`: a failed revision that changed nothing leaves `touched` False, which is
    # the common case on a template spawn -- but injection still changes runtime behavior
    # (an extra get_mark per leg per tick), so it has to face the same gate.
    if touched or injected:
        ok, reason = main_py_is_sane(strategy_dir, name, price, marks,
                                     baseline_source=_main_py_at(strategy_dir, before_head))
        print(f'main.py check for {name}: {"passed" if ok else "FAILED"} -- {reason}')
        if not ok:
            revert_main_py(strategy_dir, before_head, name)
            revised = False

    if revised and cfg_path.exists():
        cfg = json.load(open(cfg_path))
        if not _config_is_sane(cfg, price, marks, name=name):
            print(f'Revised config for {name} failed sanity check ({cfg}); treating as unrevised')
            revised = False

    fallback = None
    if not revised and cfg_path.exists():
        if seed_fallback:
            # The template ships buy_below == sell_above == 0.0, which never trades and
            # can't be nudged by apply_random_tweak (0.0 * anything is still 0.0).
            fallback = 'seeded thresholds'
            print(f'Falling back to seeded thresholds for {name}')
            apply_seed_thresholds(cfg_path, name, price)
        elif parent_cfg and Path(parent_cfg).exists():
            fallback = 'random tweak'
            print(f'Falling back to random tweak for {name}')
            apply_random_tweak(parent_cfg, cfg_path, name)

    origin = f'seed {name}' if seed_fallback else f'revise {name} from {parent_name}'
    commit_revision(strategy_dir, name,
                    f'auto: {origin} ({"llm" if revised else (fallback or "unchanged")})')

def bootstrap_initial_strategies(price, count=2, marks=None, budget=0):
    # Either the first run (strategy_state.json doesn't exist yet, so there's nothing to
    # stop or clone-from in the normal cycle) or a backfill after the population dropped
    # below KEEP_TOP_N. Seed `count` strategies straight from the template so there's
    # something for later cycles to evolve.
    # Returns the revision budget left over, so a backfill that spent the cycle's slots
    # can't have the clone loop spend them again in the same cycle.
    print(f'Bootstrapping {count} strategies from the template.')
    # Spends up to `budget` of them, not exactly one: a backfill can seed KEEP_TOP_N at
    # once and there is no reason to leave slots unused on the cycle the population
    # collapsed. Every spawn here is template-derived, so every one of them is
    # ROLE_EXPLORE.
    revise_count = min(budget, count)
    revise_idx = set(random.sample(range(count), revise_count)) if revise_count else set()
    for i in range(count):
        name = f'seed_{uuid.uuid4().hex[:12]}'
        provision_strategy(
            name, TEMPLATE_REPO,
            parent_name=name, parent_cfg=None, score=1000.0, leaderboard='{}',
            price=price, marks=marks, role=ROLE_EXPLORE,
            revise=(i in revise_idx), inject=True, seed_fallback=True,
        )
        subprocess.run(['/opt/strat_manager.py', 'start', name])
    return budget - revise_count

def apply_random_tweak(parent_cfg_path, new_cfg_path, new_name):
    # Fallback used when the master agent is unavailable or fails: copy the parent's
    # config with thresholds nudged by a small random factor (e.g. 0.95 - 1.05).
    parent = json.load(open(parent_cfg_path))
    tweak = random.uniform(0.95, 1.05)
    # Start from the parent's whole config rather than a fresh literal. This used to
    # rebuild the file from a fixed key set, so every key outside it was silently
    # deleted the moment a clone fell through to this fallback -- the template's own
    # news_veto_below, and any knob a revision had invented (rsi_period, stop_loss_pct,
    # take_profit_pct and friends are all live in the population right now). The
    # stripped config was then committed and inherited by the whole lineage, so a
    # parameter the explore role discovered could not survive a single unrevised
    # generation. Discovering new config parameters is the point of that role; erasing
    # them here made it unreachable. Note this can leave a knob behind whose main.py was
    # reverted -- inert data the next revision can see, which costs nothing.
    new_cfg_data = dict(parent)
    new_cfg_data.update({
        'name': new_name,
        'schema_version': parent.get('schema_version', 2),
        'buy_below': round(parent['buy_below'] * tweak, 6),
        'sell_above': round(parent['sell_above'] * tweak, 6),
        'trade_amount_usd': parent.get('trade_amount_usd', 10.0)
    })

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
    # Explicit both ways, so the dict copy above cannot change what `assets` means: a
    # parent whose assets value is empty or malformed produced a child with no `assets`
    # key before this function copied the parent wholesale, and still does.
    if inherited:
        new_cfg_data['assets'] = inherited
    else:
        new_cfg_data.pop('assets', None)

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
    # basis_min_bp for the same reason assets are here: it is the entire difference
    # between the gated arm and the control arm, and without it two spawns that differ
    # only in whether they trade on the basis look identical, select_parents drops one
    # as a duplicate, and the experiment loses half its population to a dedup rule.
    return (cfg.get('buy_below'), cfg.get('sell_above'),
            cfg.get('trade_amount_usd'), asset_sig, cfg.get('basis_min_bp'))

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

def _promotion_sizing(name):
    """The paper trade size this strategy was promoted on, vs the real per-trade cap.

    Recorded at promotion because the two are not the same number and nothing used to
    say so: a strategy earns the live flag on a paper track record of trade_amount_usd
    fills, then trades real money clamped by stellar_trader's caps, so the live P&L is
    not a scaled copy of the paper P&L that justified the promotion. This is only the
    *configured expectation* -- the realized ratio is per-trade and lower whenever the
    daily budget or claudio's balance binds, which is why trade_logger records it per
    trade and live_report reads it back. None on any failure; this must never block a
    promotion.
    """
    try:
        cfg = json.load(open(STRATEGIES_DIR / name / 'config.json'))
        sizing = {'trade_amount_usd': float(cfg.get('trade_amount_usd') or 0.0)}
        caps = _stellar_caps()
        if caps:
            sizing.update(caps)
            if sizing['trade_amount_usd'] > 0 and caps['max_trade_usd'] > 0:
                sizing['implied_ratio'] = round(
                    min(1.0, caps['max_trade_usd'] / sizing['trade_amount_usd']), 6)
        # Per-leg, because extra legs DO trade live now and are clamped by a different,
        # much smaller cap. Recording one ratio against MAX_TRADE_USD described the XLM
        # leg and misdescribed every other by 8x.
        legs = {}
        for a in (cfg.get('assets') or []):
            try:
                spec = f"{a['code']}:{a['issuer']}"
                want = float(a.get('trade_amount_usd') or 0.0)
            except Exception:
                continue
            leg = {'trade_amount_usd': want}
            cap = (caps or {}).get('max_trade_usd_nonbase')
            if want > 0 and cap:
                leg['implied_ratio'] = round(min(1.0, cap / want), 6)
            legs[spec] = leg
        if legs:
            sizing['legs'] = legs
        return sizing
    except Exception as e:
        print(f'could not record promotion sizing for {name} ({e})')
        return None

def save_live_strategy(name, sizing=None, trustlines=None):
    entry = {'name': name, 'since': time.time()}
    if sizing:
        entry['sizing'] = sizing
    if trustlines:
        # Which of this strategy's declared legs can actually reach real money. A leg
        # without a trustline is paper-only for as long as it stays that way, and that is
        # otherwise visible nowhere -- the strategy runs, logs trades and looks normal.
        entry['trustlines'] = trustlines
    LIVE_STRATEGY_FILE.write_text(json.dumps(entry, indent=2))


def open_trustlines_for(name):
    """Open a trustline per declared extra asset. Returns {spec: {ok, created, reason}}.

    Called once at promotion, between the outgoing strategy's wind_down and the incoming
    one's live.flag: after, so the reserve refunds from any closed leg have landed and the
    XLM floor is measured against reality; before, so a strategy is never live and trading
    into an asset whose trustline is still being opened.

    A failure is NOT fatal and must never block a promotion. ensure_trustline refuses for
    environmental reasons far more often than for unsafe ones -- Horizon unreachable,
    insufficient XLM reserve, MAX_SYSTEM_TRUSTLINES reached -- and blocking would convert
    a per-asset problem into a whole-system stall, since the cull exempts only the live
    strategy and an unpromotable leader leaves the inferior incumbent live indefinitely.
    The strategy goes live XLM-only instead, submit_trade refuses that leg per-trade with
    a reason that reaches the trade log, and the verdict is recorded in live_strategy.json.
    """
    results = {}
    try:
        cfg = json.load(open(STRATEGIES_DIR / name / 'config.json'))
    except Exception as e:
        print(f'could not read {name} config for trustlines ({e}); live XLM-only')
        return results

    tools = _tools()
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import stellar_trader
    except Exception as e:
        print(f'stellar_trader unavailable ({e}); {name} goes live XLM-only')
        return results

    # assets_from_config, not the raw list: it dedupes by spec AND code and caps at
    # MAX_EXTRA_ASSETS, so this opens trustlines for exactly what the strategy can trade.
    if tools:
        portfolio = tools[0]
        try:
            declared = portfolio.assets_from_config(cfg)
        except Exception as e:
            print(f'could not read {name} assets ({e}); live XLM-only')
            return results
    else:
        declared = cfg.get('assets') or []

    for a in declared:
        code, issuer = a.get('code'), a.get('issuer')
        if not code or not issuer:
            continue
        spec = a.get('spec') or f'{code}:{issuer}'
        try:
            r = stellar_trader.ensure_trustline(code, issuer)
        except Exception as e:
            r = {'ok': False, 'created': False, 'reason': str(e)}
        results[spec] = r
        if r.get('created'):
            print(f'  trustline opened for {spec}')
        elif r.get('ok'):
            print(f'  trustline for {spec} already present')
        else:
            print(f'  NO trustline for {spec}: {r.get("reason")} '
                  f'-- that leg stays paper-only')
    return results

def set_live_flag(name, live):
    # Plain marker file main.py polls for, not a config.json key, so it survives a
    # revision rewriting config.json from scratch. monitor.py alone writes/deletes it
    # a strategy's own code never decides if it's live
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
        if result.get('stuck'):
            # Computed by wind_down and, until now, thrown away here. A stuck leg denies
            # that asset permanently, suspends every non-XLM buy system-wide, and counts
            # toward the MAX_STUCK_USD halt -- so it silently degrades the strategy being
            # promoted right now, and the only trace was a dotfile nobody reads.
            print(f"  WARNING: {len(result['stuck'])} leg(s) left stuck and unsellable: "
                  f"{', '.join(result['stuck'])}")
            print(f"  non-XLM buys are suspended system-wide until "
                  f"/opt/trades/.stuck_positions.json is cleared by a human")
        set_live_flag(old_name, False)

    # Between wind_down and live.flag, deliberately. See open_trustlines_for.
    print(f'Promoting {current_leader} to live')
    trustlines = open_trustlines_for(current_leader)
    set_live_flag(current_leader, True)
    # Deliberately not refreshed on the "already live" re-assert path above: `since` must
    # not move, and this is a snapshot of the sizing *at promotion* by definition. A caps
    # change afterwards is exactly the drift worth seeing, so live_report flags it rather
    # than it being silently overwritten here.
    save_live_strategy(current_leader, sizing=_promotion_sizing(current_leader),
                       trustlines=trustlines)

def run():
    while True:
        print('--- Monitoring cycle', datetime.datetime.now(), '---')
        # Rolled once here, then drawn down by whichever path creates strategies this
        # cycle -- backfill or the clone loop, never both.
        budget = _revision_budget()
        print(f'Revision budget this cycle: {budget}')
        price = fetch_price_with_retry()
        if price is None:
            print(f'Could not fetch price after {PRICE_FETCH_ATTEMPTS} attempts; '
                  f'retrying this cycle in {PRICE_FAILURE_SLEEP}s')
            sleep_or_exit(PRICE_FAILURE_SLEEP, 'price retry')
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
            bootstrap_initial_strategies(price, marks=marks, budget=budget)
            sleep_or_exit(CYCLE_SLEEP)
            continue

        if len(state) < KEEP_TOP_N:
            shortfall = KEEP_TOP_N - len(state)
            print(f'Only {len(state)} strategies known (< {KEEP_TOP_N}); backfilling {shortfall} from template.')
            budget = bootstrap_initial_strategies(price, count=shortfall, marks=marks, budget=budget)
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

        # Fully swallowed, like the live/paper report below: a reporting bug must never
        # cost a monitoring cycle.
        try:
            print_turnover_report(performances)
        except Exception as e:
            print(f'turnover report unavailable ({e})')

        # One row a minute into /opt/trades/.market_history.jsonl: the DEX book, its
        # width and depth, the CEX/DEX basis and news sentiment. Nothing recorded any of
        # this before, so no strategy could condition on it and no post-mortem could
        # reconstruct the conditions a result happened under.
        try:
            ensure_market_recorder()
        except Exception as e:
            print(f'market recorder unavailable ({e})')

        # The gated-vs-control readout for the basis arm, in the log the next emperor
        # pass reads. Net worth cannot answer this question in any reasonable time --
        # it is one noisy sample an hour -- while realized venue edge is one observation
        # per fill. Swallowed like every other reporter here.
        try:
            # basis_report is NOT among the modules symlinked into /opt beside
            # monitor.py (score, strat_manager, leaderboard, live_report), which looks
            # like it should break a symlink-launched monitor and does not: CPython
            # resolves the symlink when it sets sys.path[0], so /opt/master_agent is
            # already there. This append is belt and braces, not a repair -- it keeps
            # the import working if monitor is copied rather than symlinked, or run
            # under an interpreter that leaves sys.path[0] unresolved.
            _ma = os.path.dirname(os.path.realpath(__file__))
            if _ma not in sys.path:
                sys.path.append(_ma)
            import basis_report
            print(basis_report.summary_line(24))
        except Exception as e:
            print(f'basis edge report unavailable ({e})')

        current_leader, leader_score = performances[0]
        promote_live_strategy(current_leader, leader_score)
        live_state = load_live_strategy()
        live_name = live_state['name'] if live_state else None

        # One line per cycle on how the live strategy's real fills compare to the paper
        # book it is ranked on. emperor_logs are the primary input to the next emperor
        # pass, and without this the divergence exists only in the strategy's own stdout
        # -- which is where 343 consecutive refused orders sat unnoticed on 2026-08-03.
        # Fully swallowed: a reporting bug must never cost a monitoring cycle.
        if live_name:
            try:
                import live_report
                line = live_report.summary_line(live_name)
                if line:
                    print(line)
            except Exception as e:
                print(f'live/paper report unavailable ({e})')

        # Stop everything ranked below KEEP_TOP_N (no-op if already
        # stopped). Re-derived from full-population rank every cycle, so a
        # stopped strategy can't get stuck occupying a "cull" slot forever the
        # way a fixed "worst two" window could. The live strategy is exempt even if
        # it falls out of the top N, it must not be killed while holding a real
        # pubnet position.
        for name, _ in performances[KEEP_TOP_N:]:
            info = state.get(name)
            if info and info.get('status') == 'running':
                if name == live_name:
                    print(f'Skipping cull for {name}: currently the live pubnet strategy')
                    continue
                print(f'Stopping strategy below rank {KEEP_TOP_N}: {name}')
                subprocess.run(['/opt/strat_manager.py', 'stop', name])

        # Newcomers are created every cycle (three of them now) and nothing ever removed
        # them, so the tracked population grew ~2/hour forever (74 tracked / 88 directories
        # by 2026-08-01) and every cycle re-scored dozens of identical never-traded clones.
        # Untrack the deep tail: stopped, never traded, nothing to learn from.
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

        # This cycle's newcomers: CLONES_PER_CYCLE clones of the best distinct performers,
        # plus TEMPLATE_SPAWNS_PER_CYCLE pulled fresh from the template. Built as specs up
        # front rather than provisioned as we go, because how the revision budget is spread
        # depends on the whole batch and has to be known before the first one is created.
        leaderboard = json.dumps({n: score for n, score in performances})
        specs = []
        for name, score in select_parents(performances, state, CLONES_PER_CYCLE):
            parent_repo = state[name]['path']
            specs.append(dict(
                # Only the name is intentionally not derived from the parent (so it
                # doesn't keep growing across generations of clones-of-clones); the
                # git clone itself still pulls the winning strategy's own repo.
                name=f'clone_{uuid.uuid4().hex[:12]}', source_repo=parent_repo,
                parent_name=name, parent_cfg=Path(parent_repo) / 'config.json',
                score=score, inject=False, seed_fallback=False, role=ROLE_REFINE,
            ))
        for _ in range(TEMPLATE_SPAWNS_PER_CYCLE):
            fresh = f'seed_{uuid.uuid4().hex[:12]}'
            specs.append(dict(
                name=fresh, source_repo=TEMPLATE_REPO,
                parent_name=fresh, parent_cfg=None,
                score=1000.0, inject=True, seed_fallback=True, role=ROLE_EXPLORE,
            ))

        # Every newcomer gets revised now, so there is no target draw. When the budget is
        # short of the batch (a backfill cycle already spent some of it, or
        # REVISIONS_PER_CYCLE was lowered), explore goes first: it is the scarce role and
        # the only one that can introduce logic the population does not already run.
        # Everything not revised falls back to apply_random_tweak / apply_seed_thresholds,
        # exactly as it always has when the model was unavailable.
        ordered = sorted(specs, key=lambda s: s['role'] != ROLE_EXPLORE)
        revising = {s['name'] for s in ordered[:budget]}
        print(f'Revision budget {budget}/{len(specs)}; revising '
              f'{", ".join(sorted(revising)) or "nothing"} this cycle '
              f'({", ".join(s["name"] + "/" + s["role"] for s in specs)})')
        for spec in specs:
            provision_strategy(**spec, leaderboard=leaderboard, price=price, marks=marks,
                               revise=(spec['name'] in revising))

        # Start the newcomers, plus any top-N strategy that's currently stopped
        # (reload state first since strat_manager.py clone/stop wrote to disk)
        for spec in specs:
            subprocess.run(['/opt/strat_manager.py', 'start', spec['name']])

        fresh_state = load_state()
        for name, _ in performances[:KEEP_TOP_N]:
            info = fresh_state.get(name)
            if info and info.get('status') == 'stopped':
                print(f'Restarting top-{KEEP_TOP_N} strategy that was stopped: {name}')
                subprocess.run(['/opt/strat_manager.py', 'start', name])

        # Wait an hour before next cycle (or exit here if asked to stop)
        sleep_or_exit(CYCLE_SLEEP)

USAGE = """usage: monitor.py [--ensure-recorder]

  (no arguments)      run the evolutionary loop forever, one cycle per hour
  --ensure-recorder   start the market recorder daemon if it is not already
                      running, print the market-history report, and exit.
                      Exits 1 if the recorder could not be confirmed running.
"""

if __name__ == '__main__':
    args = sys.argv[1:]
    # Argument handling exists mostly so a typo cannot start the loop. Anything
    # unrecognised (including --help, which used to fall through here and begin a
    # full cycle) prints usage instead of trading.
    if not args:
        run()
    elif args == ['--ensure-recorder']:
        # Standalone supervision, for once.sh / cron / a hand check. The daemon is
        # setsid'd, so it outlives this process exiting seconds later exactly as it
        # outlives a monitor cycle.
        try:
            ensure_market_recorder()
        except Exception as e:
            print(f'market recorder unavailable ({e})')
        alive = _recorder_alive()
        print(f'recorder running: {alive}')
        sys.exit(0 if alive else 1)
    else:
        print(USAGE, end='')
        sys.exit(0 if args in (['-h'], ['--help']) else 2)

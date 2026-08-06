#!/usr/bin/env python3
"""The evolutionary loop. Runs forever, one cycle per hour.

Every cycle it ranks *all* known strategies (running or stopped) by the active domain's
score, stops anything ranked below KEEP_TOP_N, and adds three newcomers: CLONES_PER_CYCLE
clones of the best distinct performers plus TEMPLATE_SPAWNS_PER_CYCLE pulled fresh from
the domain's template repo, all with slightly tweaked/revised parameters. It then makes
sure those plus the rest of the top N are running -- so the rank-based cull stops three per
cycle simply because three are added. Up to REVISIONS_PER_CYCLE of the newcomers are handed
to the LLM (and only on REVISION_CHANCE of cycles) -- currently all of them -- each with a
prompt matching its role, ROLE_REFINE for a clone or ROLE_EXPLORE for a template spawn; see
_revision_budget and domain.ROLE_*.
If the population is below KEEP_TOP_N (e.g. after strategies were removed via
`strat_manager.py rm`), it backfills the shortfall from the template before scoring.

To stop it, touch /opt/.monitor.py.exit: it exits at its next between-cycle sleep
rather than being signalled mid-revision (see EXIT_FILE / sleep_or_exit below).

THIS FILE KNOWS NOTHING ABOUT WHAT IT IS TRADING, AND MUST STAY THAT WAY.
========================================================================
Prices, order books, assets, issuers, threshold bands, spreads, trustlines and the
real-money caps all live behind `DOMAIN` -- see domain.py for the contract and
domain_sdex.py for the Stellar DEX implementation. If you are about to add one of those
things here, it belongs in the domain module instead. `obs`, the per-cycle observation
threaded through this file, is deliberately opaque: the loop only ever tests it for None
and hands it back. Do not read an attribute off it.

Where the domain went, for anything (or anyone) looking for a name that used to be here:

    get_current_price/fetch_price_with_retry  DOMAIN.observe()          -> obs | None
    fetch_marks_for_cycle(state, price)       DOMAIN.observe_population(obs, state)
    score_from_strategy_path(path, price, m)  DOMAIN.score_path(path, obs)
    trade_log_path / trade_stats              DOMAIN.activity_log_path / DOMAIN.activity
    turnover_stats / print_turnover_report    DOMAIN.turnover_stats / DOMAIN.report_activity
    _config_signature                         DOMAIN.config_signature
    _stellar_caps                             DOMAIN.caps()
    _importability_check                      DOMAIN.importability()
    _backtest_trade_count(dir)                DOMAIN.replay(dir)['trades']
    _recorder_alive / ensure_market_recorder  DOMAIN.background_jobs_alive / ensure_background_jobs
    _normalize_config                         DOMAIN.normalize_config(path, name)
    _sanitize_assets(path, marks)             DOMAIN.sanitize_config(path, obs)
    _repair_config(path, price, name)         DOMAIN.repair_config(path, name, obs)
    _config_is_sane(cfg, price, marks, name)  DOMAIN.config_is_sane(cfg, name, obs)
    _assets_are_sane / _thresholds_are_sane   (inside DOMAIN.config_is_sane)
    _basis_gate_is_sane / _required_keys_...  (inside DOMAIN.config_is_sane)
    _inject_discovered_assets/_basis_gate     DOMAIN.inject_experiments(path, obs)
    apply_seed_thresholds / _seed_band_...    DOMAIN.seed_config(path, name, obs)
    apply_random_tweak                        DOMAIN.tweak_config(parent, new, name)
    main_py_calls_execute_trade               DOMAIN.can_execute_live(name)
    _promotion_sizing / open_trustlines_for   DOMAIN.promotion_sizing / DOMAIN.prepare_live
    MAIN_PY_IMPORTABILITY                     domain_sdex.MAIN_PY_IMPORTABILITY
    BACKTEST_GATE_DAYS                        DOMAIN.REPLAY_DAYS
    REFLECTOR_INJECT_* / BASIS_INJECT_*       domain_sdex (inject_experiments)
    RECORDER_*                                domain_sdex (ensure_background_jobs)

There are deliberately no back-compat aliases: several of these changed signature, and an
alias that accepts the old arguments and means something else is a worse trap than a
NameError. main_py_is_sane, provision_strategy, promote_live_strategy, qualifies_for_live
and run() all kept their names. Some comments in score.py, live_report.py, basis_report.py,
strat_manager.py and tools/ still say "monitor.<oldname>"; the table above is the
translation, and they were left alone rather than churn two git repos the integrity check
watches.

Three sdex-flavoured names are knowingly still in this file, because changing them changes
either an on-disk format or a log line, and both would spoil the diff that proves this
refactor was inert: `trustlines` (the live_strategy.json key DOMAIN.prepare_live's result
is stored under, which live_report.py reads back), and the "regime summary unavailable" /
"basis edge report unavailable" messages on the reporter try/excepts. Fix them in the
first commit after a cycle-log diff has been signed off, not before.
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
import traceback
import datetime
import random
import uuid
from pathlib import Path

import domain

# Resolved once, at import: a domain that cannot be imported is a total failure of the
# run and should say so before a cycle starts. emperor.sh already notices a monitor that
# exits after only a few seconds, whereas a lazy import raising mid-cycle takes down
# scoring, culling and spawning for the whole population and reads as one bad cycle.
DOMAIN = domain.get()

# The filesystem contract, defined in domain.py because both sides need it.
STATE_FILE = domain.STATE_FILE
STRATEGIES_DIR = domain.STRATEGIES_DIR
TRADES_DIR = domain.TRADES_DIR
LIVE_STRATEGY_FILE = domain.LIVE_STRATEGY_FILE   # which single strategy trades real money
ROLE_REFINE = domain.ROLE_REFINE
ROLE_EXPLORE = domain.ROLE_EXPLORE

MASTER_AGENT_SCRIPT = Path('/opt/master_agent/master-agent.py')
# Override with the REVISION_TIMEOUT env var rather than editing this literal -- it has
# been flipped back and forth by successive emperor passes. The right value depends on
# which model master-agent.py is pointed at (see MASTER_AGENT_MODEL there): the cloud
# model fails or answers in minutes, the local ones can take hours.
REVISION_TIMEOUT = int(os.environ.get('REVISION_TIMEOUT', 60000))
KEEP_TOP_N = 8 # strategies ranked below this by score get stopped each cycle
RETIRE_BELOW_RANK = KEEP_TOP_N * 3 # stopped, never-acted strategies below this get untracked

# How long a strategy may run without ever logging an action before it stops being ranked
# on its score and is sorted below everything that has actually acted.
#
# This is the third time the loop has ended up cloning strategies that do nothing, and
# the first time the cause was not the scoring formula. A strategy that never trades
# holds exactly its $1000 starting balance forever, so its score is a constant. When XLM
# is falling -- it was down 15.8% over the 30 days to 2026-08-05, and 18.2% off the
# window high -- every strategy that put capital to work marks below $1000 and every
# strategy whose band missed the market sits exactly at it. Measured that morning: 9 of
# 32 tracked strategies had never logged a trade, several after 30+ hours, and they held
# 9 of the top 10 leaderboard slots. select_parents cloned them, the cull protected them,
# and RETIRE_BELOW_RANK could never reach them because retirement only looks below rank
# 24. The population was converging on strategies whose configured bands do not intersect
# the market at all (buy_below 0.164077 against a 0.16618 spot, and so on).
#
# Ranking them last is deliberately not the same as deleting them: they keep their score,
# they are still listed, and the existing cull + RETIRE_BELOW_RANK path is what actually
# reaps them a cycle later. A strategy is only demoted once it has had IDLE_GRACE_S of
# real market to act in, so a fresh clone is never punished for being new.
IDLE_GRACE_S = int(os.environ.get('IDLE_GRACE_S', 3 * 3600))

# Repos whose contents decide how real money moves. Watched every cycle by
# check_boundary_integrity(); see that function for why. Not the domain's business: these
# watch *this system's* code, including the domain module itself.
INTEGRITY_REPOS = [Path('/opt/tools'), Path('/opt/master_agent')]
INTEGRITY_BASELINE = Path('/opt/.integrity_baseline.json')

# Guardrails on handing a strategy the real-money live flag. On 2026-08-01 the live flag
# was given to a clone with zero trades and zero track record, purely because 45 clones
# that had never traded were tied at the top of a broken ranking. Rank alone is not
# enough evidence to trade real money on: require a demonstrated, profitable history.
#
# MIN_LIVE_SCORE is deliberately NOT collapsed into DOMAIN.STARTING_SCORE even though the
# two are currently equal. This one is promotion policy -- "must actually be up on where
# it started" -- and a domain is free to decide a strategy starts at 0.
MIN_LIVE_TRADES = 20        # actions logged before a strategy may go live
MIN_LIVE_AGE_S = 2 * 3600   # seconds between its first and last action
MIN_LIVE_SCORE = 1000.0     # must actually be up on its starting balance

# How long a revised main.py gets to prove it runs and persists state before the gate
# gives up on it. Must comfortably exceed one loop iteration (the template fetches a
# price, then writes state.json, then sleeps 30s) plus a slow price-feed failover.
SMOKE_TEST_SECONDS = int(os.environ.get('SMOKE_TEST_SECONDS', 120))

# What one cycle adds to the population: two clones of the best distinct performers, plus
# one strategy pulled fresh from the domain's template. The template spawn exists because
# every other newcomer descends from an existing strategy -- without it the population can
# only ever narrow around whatever the current leaders already do, and
# bootstrap_initial_strategies (the only other template path) fires just on first boot or a
# below-KEEP_TOP_N backfill.
# The cull is rank-based (see the performances[KEEP_TOP_N:] loop in run()), so adding three
# per cycle is what makes it stop three per cycle; there is no separate "how many to stop".
CLONES_PER_CYCLE = 2
TEMPLATE_SPAWNS_PER_CYCLE = 1

# Cap on revise-strategy subprocess calls per cycle, and the odds a cycle spends them at
# all. Every newcomer this cycle gets a revision. At 1 -- with the target drawn by a
# uniform random.choice over a batch that is two clones to one template spawn -- the
# fresh spawn, which is the only channel that can introduce logic the population does not
# already run, won the draw on 0.75 * 1/3 = 1/4 of cycles; every other newcomer inherited
# a parent's main.py verbatim, since the mechanical fallback only scales parameters.
# Measured result: 141 main.py files across the population, ~10 distinct hashes, the top
# three covering 113 of them.
#
# Note this is deliberately NOT paired with a lower REVISION_TIMEOUT (still 60000s,
# ~16.7h): three sequential revisions may legally run far past CYCLE_SLEEP and outlast an
# emperor.sh window, which is the risk the cap of 1 originally existed to avoid. That is
# an accepted trade, and it only binds if the model hangs -- REVISION_TIMEOUT reads from
# the environment, so it can be lowered without a code edit if cycles run long.
#
# The ~25% of cycles that revise nothing are not a failure mode -- every newcomer still
# gets DOMAIN.tweak_config or DOMAIN.seed_config, the same exploration the fallback path
# has always done.
REVISIONS_PER_CYCLE = 3
REVISION_CHANCE = 0.75

OBSERVE_FAILURE_SLEEP = 300   # seconds to wait before retrying a cycle that got no obs
CYCLE_SLEEP = 3600

# Cooperative shutdown request. Touch this file and monitor.py exits the next time it
# reaches a cycle-boundary sleep instead of sleeping. This is how emperor.sh ends a
# monitor window now: a SIGTERM to the process group can land anywhere, including in the
# middle of an in-flight revise-strategy subprocess, a DOMAIN.sanitize_config rewrite of a
# clone's config.json, or a commit_revision git commit -- leaving a half-revised clone
# that the next cycle then has to prune. Checked only at the sleeps, so by construction
# nothing is in flight when it fires. emperor.sh still falls back to TERM if this is
# ignored (the wait there has to exceed CYCLE_SLEEP, since a monitor that is already
# asleep will not notice the file until the sleep ends).
EXIT_FILE = Path('/opt/.monitor.py.exit')


def _strategy_python():
    """Which interpreter a strategy is really started under.

    Resolved by strat_manager, the process that actually starts them, so the smoke test
    can never gate on a different environment than the run. Imported at call time rather
    than module scope because strat_manager mkdirs /opt/strategies as an import side
    effect, which would make this file unimportable outside the container -- and
    selftest_domain.py has to be able to import it.
    """
    from strat_manager import _strategy_python as resolve
    return resolve()


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

def compute_strategy_score(strategy_name, state_entry, obs):
    score = DOMAIN.score_path(state_entry['path'], obs)
    if score is None:
        print(f'Error reading state for {strategy_name}')
        return -float('inf')
    return score


def strategy_age_s(state_entry):
    """Seconds since this strategy's directory was created, or None.

    The directory is created by `git clone` in strat_manager and never recreated, so its
    ctime is the strategy's birth. config.json's mtime would not do -- a revision rewrites
    it -- and state.json's is refreshed every 30s by the running loop.

    Overstates opportunity slightly for a strategy that spent part of its life stopped by
    the cull. That is the right direction for the only thing this is used for: deciding
    when a strategy has had enough market to prove it can act at all.
    """
    try:
        return max(0.0, time.time() - os.stat(state_entry['path']).st_ctime)
    except Exception:
        return None


def is_idle(name, trades, state_entry):
    """True if this strategy has had IDLE_GRACE_S to act and has not acted.

    Split out from the ranking so the meaning stays in one place: "idle" here is
    specifically *never logged a single action in its whole life*, not "is currently
    flat" and not "has stopped acting recently". A strategy that acted once and then
    went quiet still ranks on its score -- it demonstrated its logic can fire, and
    whether being flat is the right call is exactly what the score is for.
    """
    if trades:
        return False
    age = strategy_age_s(state_entry)
    return age is not None and age >= IDLE_GRACE_S


def print_idle_report(performances, state, trade_counts, obs):
    """Who in this population can still act, and who has painted itself into a corner.

    Two distinct dead ends, both invisible to the score, both measured on 2026-08-05. The
    first is domain-independent and lives here:

      * IDLE -- never acted. 9 of 32, some 30+ hours old, holding $1000 in cash because
        their configured band does not intersect the market. Their score is a constant at
        exactly the starting balance, which in a falling market is the top of the
        leaderboard. See IDLE_GRACE_S.

    The second is not: "has painted itself into a corner" means something different in
    every domain, so it comes from DOMAIN.stuck_report -- for sdex, all-in and unable to
    either buy or sell. Both blocks are reported rather than acted on, except through the
    ranking demotion idle strategies already get.
    """
    idle = []
    for name, _score in performances:
        entry = state.get(name)
        if not entry:
            continue
        trades = trade_counts.get(name, 0)
        if is_idle(name, trades, entry):
            age = strategy_age_s(entry) or 0
            idle.append(f'{name} ({age / 3600:.0f}h)')
    if idle:
        print(f'Idle: {len(idle)} of {len(performances)} strategies have never acted after '
              f'{IDLE_GRACE_S / 3600:.0f}h (ranked below every strategy that has): '
              f'{domain.first_few(idle)}')
    stuck = DOMAIN.stuck_report(performances, state, obs)
    if stuck:
        print(stuck)


def main_py_is_sane(strategy_dir, name, obs, *, baseline_source=None):
    """Check that a revised main.py actually parses, runs, and persists state.

    DOMAIN.config_is_sane only ever looks at config.json, so a revision that gutted
    main.py sailed straight through and got started. On 2026-08-01 seed_0_1785619070
    was handed to a model that wrote main.py eight times, each attempt a syntax
    error, shrinking 3892 -> 377 bytes until the file finally *parsed* -- a stub
    with no trading loop that exits immediately and never writes state.json. It
    scored -inf (0 trades) forever after, and the config-only gate never noticed.

    Returns (ok, reason).

    This is the generic harness. Three things about it are the domain's and are asked as
    hooks: whether the candidate is still legible to the domain's replay engine
    (DOMAIN.check_replayable), what a config needs before it can run at all
    (DOMAIN.prepare_smoke_config), and whether the state it wrote is legal
    (DOMAIN.check_smoke_state). A fourth, DOMAIN.cleanup_scratch, removes whatever the run
    left outside its temp directory.

    `baseline_source` is the main.py this candidate would be reverted to (see
    _main_py_at). It is only passed to the replayability hook, which refuses a revision
    that would take a lineage from replay-visible to replay-blind.

    The smoke run happens in a throwaway copy, never the real directory: for a domain
    with money, execution submits a REAL order when a live.flag exists in cwd, and the
    action log it appends to feeds the counts gating real-money promotion. So the copy
    drops live.flag, runs under DOMAIN.SMOKE_ENV (which must make execution structurally
    impossible), and acts under a scratch name whose log is deleted afterwards.
    """
    main_py = Path(strategy_dir) / 'main.py'
    if not main_py.exists():
        return False, 'main.py is missing'

    source = main_py.read_text()
    try:
        ast.parse(source)
    except SyntaxError as e:
        return False, f'main.py has a syntax error: {e.msg} (line {e.lineno})'

    # Before the smoke run, deliberately: the hook is a pure AST walk costing
    # microseconds, the smoke run costs SMOKE_TEST_SECONDS (120s) per candidate, and "it
    # also runs" adds nothing once the verdict is decided. The hook's reason string names
    # the offending line, because "not importable" on its own is unactionable to the next
    # revision. It returns None for "no verdict" -- including on any internal failure,
    # since it guards a fitness signal rather than money.
    verdict = DOMAIN.check_replayable(source, baseline_source, name)
    if verdict is not None and not verdict[0]:
        return False, verdict[1]

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
        cfg = DOMAIN.prepare_smoke_config(cfg, obs)
        json.dump(cfg, open(tmp_dir / 'config.json', 'w'), indent=2)

        # DOMAIN.SMOKE_ENV makes the no-live-trading guarantee structural rather than
        # relying on having remembered to drop live.flag from the copy.
        env = dict(
            os.environ,
            **DOMAIN.SMOKE_ENV,
            PYTHONPATH=os.pathsep.join(filter(None, ['/opt/tools', os.environ.get('PYTHONPATH')]))
        )
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
            # strategy that returns is simply dead and can never act again.
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

        ok, detail = DOMAIN.check_smoke_state(raw, cfg, obs)
        if not ok:
            return False, detail
        return True, f'ran {SMOKE_TEST_SECONDS}s, {detail}'
    except Exception as e:
        return False, f'smoke test errored: {e}'
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        DOMAIN.cleanup_scratch(scratch_name)

def revert_main_py(strategy_dir, before_head, name):
    """Restore the parent's main.py after a failed gate, keeping any other revision.

    Reverts to the commit the clone started at rather than HEAD, since the model may
    have already committed the broken main.py onto its own branch.
    """
    _git(strategy_dir, 'stash', 'push', '--', 'main.py')
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
    DOMAIN.config_is_sane, neither of which notices that the model edited files and
    never ran git -- the commit requirement lived only as prompt text in
    master-agent.py, and was ignored often enough that most strategies on disk
    sat on master with an uncommitted config.json. That doesn't hurt the clone
    itself (main.py reads the working tree) but it silently breaks the next
    generation: strat_manager.py clones a parent with plain `git clone`, which
    copies committed history only, so the winning parameters are replaced by
    whatever HEAD still holds -- usually the template's never-trading 0.0s.

    Commit unconditionally, whoever wrote the change (LLM or a mechanical fallback),
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


def _revision_budget():
    """How many revise-strategy calls this cycle may make: REVISIONS_PER_CYCLE or 0.

    Rolled once per cycle, deliberately -- not once per candidate. The budget is shared by
    every path that can create a strategy (the clone loop and bootstrap_initial_strategies
    alike), so the REVISIONS_PER_CYCLE ceiling holds even on a backfill cycle that seeds
    eight strategies at once. That used to be eight back-to-back REVISION_TIMEOUT waits.

    All-or-nothing, not a per-candidate coin flip: the ~25% of cycles that return 0 are
    the control arm, a whole batch built by DOMAIN.tweak_config / DOMAIN.seed_config
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
    LLM layer was dead, monitor fell through to a mechanical tweak on every clone, and
    the system's "evolution" was random parameter jitter with no model in the loop. From
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


def _run_revision(name, parent_name, score, leaderboard, obs, role=ROLE_REFINE):
    """Hand one strategy to `master-agent.py revise-strategy`. True only on a clean exit.

    The single call site for the revision subprocess. master-agent.py already turns a
    model error into a non-zero exit (a reply starting with '[error:'), so a False here
    means the caller must fall back to a mechanical tweak rather than start a clone that
    is byte-identical to its parent.

    `role` selects which user prompt master-agent.py builds -- see domain.ROLE_REFINE /
    ROLE_EXPLORE. Passed as a trailing positional because that end of master-agent.py is
    a bare `revise_strategy(*sys.argv[2:])` splat with defaults.

    The observation crosses as ONE argv token via DOMAIN.encode_observation, and the far
    side rebuilds it with DOMAIN.decode_observation. Watch this seam: a decode mismatch
    makes revise_strategy exit non-zero, this return False, and every newcomer fall back
    to a mechanical tweak -- which reads in the log as an ordinary cycle. That is the same
    silent shape as the interpreter bug above, and selftest_domain.py asserts the round
    trip for exactly that reason.
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
             name, parent_name, str(score), leaderboard,
             DOMAIN.encode_observation(obs), role],
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
                       obs, revise, inject, seed_fallback, role=None):
    """Create one strategy: clone -> optional revision -> gates -> fallback -> commit.

    The one path every new strategy takes, whether it is a clone of a winner or a fresh
    pull from the domain's template. Only four things differ between those two cases, and
    they are the keyword arguments: `inject` (offer it the domain's mechanical
    experiments), `seed_fallback` (rebuild config.json from the current observation instead
    of nudging a parent's), `role` (which prompt the revision model is given), and `revise`
    (spend one of the cycle's revision slots on this one).

    Deliberately does not start the strategy -- callers do, because the clone loop starts
    its whole batch only after every clone has been gated and committed.
    """
    # Defaulted rather than required so a caller that forgets it still gets the right
    # prompt: parent_name == name is exactly the template-spawn case, and it is the one
    # the refine prompt degenerates on ("a new clone `seed_x` of `seed_x`", over an empty
    # state.json and a config whose parameters are all 0.0).
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

    revised = _run_revision(name, parent_name, score, leaderboard, obs, role) if revise else False

    touched = revision_changed_anything(strategy_dir, before_head)
    if revised and not touched:
        print(f'Revision for {name} left the clone identical to {parent_name}; treating as unrevised')
        revised = False

    # The one place novelty the population cannot invent enters mechanically rather than
    # by the revision model choosing to write it -- DOMAIN.sanitize_config only ever
    # removes, and DOMAIN.tweak_config only copies the parent's forward. Before
    # sanitizing, so anything injected goes through the exact same verification gate an
    # LLM-chosen value does. Template-derived spawns only: clones inherit their parent's
    # arm, which is what makes a lineage stay in the arm it was born into.
    injected = False
    if inject and cfg_path.exists():
        injected = DOMAIN.inject_experiments(cfg_path, obs)

    # Supply any required key the config is missing before the gate asks for them. Here
    # rather than before the revision, deliberately: this writes config.json, and
    # `touched` above is a dirty-tree check -- normalizing any earlier would mark every
    # clone as touched and force the SMOKE_TEST_SECONDS smoke run on clones nothing had
    # actually changed. Same reason sanitize_config sits here.
    if cfg_path.exists():
        DOMAIN.normalize_config(cfg_path, name)

    # Always re-verify whatever the domain admits, revised or not: config.json is
    # committed to git and inherited by every future clone of this lineage,
    # DOMAIN.tweak_config copies it forward verbatim, and something that was fine when
    # admitted can go bad later. Before the smoke test, so the smoke run exercises the
    # config the strategy will actually start with.
    if cfg_path.exists():
        DOMAIN.sanitize_config(cfg_path, obs)

    # Repair what the domain can derive itself, before the gate decides whether to throw
    # the whole revision away. Here and not inside the `revised` branch below, and for the
    # same reason sanitize_config is unconditional: a bad knob is committed to git and
    # inherited by the whole lineage, and both fallbacks copy the existing config forward
    # rather than rebuilding it, so a value the gate rejects otherwise survives the
    # rejection. See DOMAIN.repair_config for the case that proved it.
    if cfg_path.exists():
        DOMAIN.repair_config(cfg_path, name, obs)

    # Runs whenever the model touched the repo, even if `revised` is already False: both
    # fallbacks only rewrite config.json, so a gutted main.py would otherwise survive a
    # failed config check and get started anyway (seed_0_1785619070).
    # `or injected`: a failed revision that changed nothing leaves `touched` False, which is
    # the common case on a template spawn -- but injection still changes runtime behavior,
    # so it has to face the same gate.
    if touched or injected:
        ok, reason = main_py_is_sane(strategy_dir, name, obs,
                                     baseline_source=_main_py_at(strategy_dir, before_head))
        print(f'main.py check for {name}: {"passed" if ok else "FAILED"} -- {reason}')
        if not ok:
            revert_main_py(strategy_dir, before_head, name)
            revised = False

    # Unparseable is a sanity failure, not a crash. normalize_config and sanitize_config
    # both already swallow a bad parse and return early on the stated assumption that
    # "config_is_sane will fail it and the fallback rebuilds the file from scratch" -- but
    # this load was bare, so a revision that wrote invalid JSON took the whole monitor
    # process down here instead, killing the cycle and every strategy's evolution until
    # the next emperor window. Also rejects a parseable non-object: the required-keys
    # check calls cfg.get and would raise on a list.
    if revised and cfg_path.exists():
        cfg = None
        try:
            cfg = json.load(open(cfg_path))
        except Exception as e:
            print(f'Revised config for {name} is unreadable ({e}); treating as unrevised')
            revised = False
        if revised and not isinstance(cfg, dict):
            print(f'Revised config for {name} is a {type(cfg).__name__}, not an object; '
                  f'treating as unrevised')
            revised = False
        if revised and not DOMAIN.config_is_sane(cfg, name, obs):
            print(f'Revised config for {name} failed sanity check ({cfg}); treating as unrevised')
            revised = False

    # Would this thing ever act? Last of the gates, because it is the only one that needs
    # the finished article: config and main.py all settled. A revision that replays zero
    # actions over the domain's whole replay window has not written a strategy, and the
    # model has repeatedly shipped exactly that while quoting `beats_buy_hold: true` as
    # its justification -- see DOMAIN.replay. Demoting it to "unrevised" routes it through
    # the fallback below, which rebuilds parameters that do fire.
    if revised:
        result = DOMAIN.replay(strategy_dir)
        fired = result['trades'] if result else None
        if fired == 0:
            print(f'Revised {name} replays 0 trades over {DOMAIN.REPLAY_WINDOW}; '
                  f'treating as unrevised')
            revised = False

    fallback = None
    if not revised and cfg_path.exists():
        if seed_fallback:
            # The template ships parameters that never act and cannot be nudged by
            # tweak_config (0.0 * anything is still 0.0).
            fallback = 'seeded thresholds'
            print(f'Falling back to seeded thresholds for {name}')
            DOMAIN.seed_config(cfg_path, name, obs)
        elif parent_cfg and Path(parent_cfg).exists():
            fallback = 'random tweak'
            print(f'Falling back to random tweak for {name}')
            # A tweak can only nudge a config it can read, and a parent's config.json is
            # not guaranteed readable -- it is whatever that lineage last committed. On
            # failure fall through to a seeded config rather than leaving the clone's
            # own (possibly unparseable) config.json to be committed and started: every
            # not-revised path has to end with a config monitor built itself.
            if not DOMAIN.tweak_config(parent_cfg, cfg_path, name):
                fallback = 'seeded thresholds (unreadable parent config)'
                print(f'Parent config for {name} is unusable; seeding thresholds instead')
                DOMAIN.seed_config(cfg_path, name, obs)

    # One line per newcomer saying whether the thing about to be started can fire at all.
    # After the fallback, so it describes the config that will actually run. A 0 here is
    # not fatal -- a fallback config is monitor's own work and reverting it has nothing to
    # revert to -- but it is the earliest possible warning that this strategy will spend
    # its life at exactly its starting score, and it belongs in the log the next emperor
    # pass reads.
    result = DOMAIN.replay(strategy_dir)
    fired = result['trades'] if result else None
    if fired is not None:
        note = '  <-- will never trade; it is starting sterile' if fired == 0 else ''
        print(f'Backtest for {name}: {fired} trades over {DOMAIN.REPLAY_DAYS}d{note}')

    origin = f'seed {name}' if seed_fallback else f'revise {name} from {parent_name}'
    commit_revision(strategy_dir, name,
                    f'auto: {origin} ({"llm" if revised else (fallback or "unchanged")})')

def bootstrap_initial_strategies(obs, count=2, budget=0):
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
        # Same reasoning as the clone loop in run(): one failed seed skips itself rather
        # than taking down the backfill, which is running precisely because the
        # population is already below KEEP_TOP_N.
        try:
            provision_strategy(
                name, DOMAIN.TEMPLATE_REPO,
                parent_name=name, parent_cfg=None, score=DOMAIN.STARTING_SCORE,
                leaderboard='{}', obs=obs, role=ROLE_EXPLORE,
                revise=(i in revise_idx), inject=True, seed_fallback=True,
            )
        except Exception as e:
            traceback.print_exc()
            print(f'Bootstrapping {name} failed ({e}); skipping it')
            continue
        subprocess.run(['/opt/strat_manager.py', 'start', name])
    return budget - revise_count


def select_parents(performances, state, count=2):
    """Pick `count` distinct strategies to clone and revise this cycle.

    Taking performances[:count] outright kept picking two clones with byte-identical
    configs (the monitor logs show the same one or two parents cloned hour after hour),
    which spends both revision slots exploring the same point. Prefer the best scorer,
    then the best remaining one whose config actually differs; fall back to plain rank
    order if everything looks the same.

    "Actually differs" is the domain's call -- DOMAIN.config_signature -- because which
    parameters define a strategy's behaviour is exactly what a domain is.
    """
    chosen = []
    seen_signatures = []
    for name, score in performances:
        if name not in state:
            continue
        signature = DOMAIN.config_signature(state[name])
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

def set_live_flag(name, live):
    # Plain marker file main.py polls for, not a config.json key, so it survives a
    # revision rewriting config.json from scratch. monitor.py alone writes/deletes it
    # a strategy's own code never decides if it's live
    flag_path = STRATEGIES_DIR / name / 'live.flag'
    if live:
        flag_path.touch()
    else:
        flag_path.unlink(missing_ok=True)


def check_boundary_integrity():
    """Has the real-money safety boundary been modified out from under us? (ok, reasons)

    The domain's caps are the only thing enforcing how much real money can move, and they
    are deliberately not caller-supplied so a revision cannot override them. That argument
    holds only as long as the files themselves are fixed -- and the revision agent runs
    `exec` as root with no path restriction and is explicitly told it may edit its own
    tooling. Nothing structurally prevents it rewriting the caps or the trading code.
    Until that hole is closed properly (an unprivileged revision user, or a read-only
    mount), this is the detection layer: notice when the boundary changes and stop trading
    real money rather than trusting it.

    Watches /opt/master_agent as well as /opt/tools, so the domain module itself -- which
    now holds the promotion gates and reads the caps -- is inside the boundary.

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
    has never acted sits at exactly its starting balance, which can top the ranking
    on any quiet day. Real money only goes to a strategy with a real, profitable
    track record. If nothing qualifies, nothing is promoted and no live trading
    happens this cycle -- that is the intended safe default.
    """
    # Checked first: this is about whether the strategy *can* act live at all, which
    # no amount of paper track record makes up for. The only domain member that fails
    # closed -- see domain.py.
    can_execute, why_not = DOMAIN.can_execute_live(name)
    if not can_execute:
        return False, why_not
    count, first, last = DOMAIN.activity(name)
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
    is gated on DOMAIN.retire_live() fully flattening the outgoing strategy's real
    position first — if it can't finish in one cycle, the old leader simply stays live
    and this retries next cycle (safe: run()'s cull loop exempts the live strategy from
    KEEP_TOP_N below).
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
            # do NOT retire_live: flattening would route through the very code whose
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
        flattened, lines = DOMAIN.retire_live(old_name)
        for line in lines:
            print(line)
        if not flattened:
            return
        set_live_flag(old_name, False)

    # Between retire_live and live.flag, deliberately. See DOMAIN.prepare_live.
    print(f'Promoting {current_leader} to live')
    trustlines = DOMAIN.prepare_live(current_leader)
    set_live_flag(current_leader, True)
    # Deliberately not refreshed on the "already live" re-assert path above: `since` must
    # not move, and this is a snapshot of the sizing *at promotion* by definition. A caps
    # change afterwards is exactly the drift worth seeing, so live_report flags it rather
    # than it being silently overwritten here.
    save_live_strategy(current_leader, sizing=DOMAIN.promotion_sizing(current_leader),
                       trustlines=trustlines)

def run():
    while True:
        print('--- Monitoring cycle', datetime.datetime.now(), '---')
        # Rolled once here, then drawn down by whichever path creates strategies this
        # cycle -- backfill or the clone loop, never both.
        budget = _revision_budget()
        print(f'Revision budget this cycle: {budget}')
        obs = DOMAIN.observe()
        if obs is None:
            print(f'{DOMAIN.OBSERVE_FAILURE_NOTE}; '
                  f'retrying this cycle in {OBSERVE_FAILURE_SLEEP}s')
            sleep_or_exit(OBSERVE_FAILURE_SLEEP, 'observation retry')
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
        # Whatever per-population data the domain needs, fetched once per cycle before
        # any scoring. Doing it per strategy would mean hundreds of blocking network
        # calls per ranking pass.
        obs = DOMAIN.observe_population(obs, state)
        if not state:
            bootstrap_initial_strategies(obs, budget=budget)
            sleep_or_exit(CYCLE_SLEEP)
            continue

        if len(state) < KEEP_TOP_N:
            shortfall = KEEP_TOP_N - len(state)
            print(f'Only {len(state)} strategies known (< {KEEP_TOP_N}); backfilling {shortfall} from template.')
            budget = bootstrap_initial_strategies(obs, count=shortfall, budget=budget)
            state = load_state()

        performances = []
        trade_counts = {}
        idle_names = set()
        for name, info in state.items():
            score = compute_strategy_score(name, info, obs)
            trade_counts[name] = DOMAIN.activity(name)[0]
            if is_idle(name, trade_counts[name], info):
                idle_names.add(name)
            performances.append((name, score))
        # Sort key, outermost first:
        #
        # 1. Has it ever acted (given IDLE_GRACE_S to do so)? A strategy that has not is
        #    ranked below every strategy that has, whatever its score. Its score is not a
        #    result -- it is the starting balance, unchanged, and in a falling market that
        #    beats every strategy holding the asset. See IDLE_GRACE_S for the measurement.
        #    This is what stops the loop cloning its own dead ends.
        # 2. Score, as always.
        # 3. Action count, to break exact ties. Strategies inside the grace period all sit
        #    at exactly their starting balance, so without this the ordering of a 45-way
        #    tie is just the insertion order of strategy_state.json -- the same names won
        #    and lost every cycle, and the "best" strategy handed to the revision agent
        #    was arbitrary. Among equals, prefer the one that has demonstrated something.
        performances.sort(key=lambda x: (x[0] not in idle_names, x[1],
                                         trade_counts.get(x[0], 0)), reverse=True)
        print('Strategy performances (score):')
        for name, score in performances:
            tag = '  [idle: never traded]' if name in idle_names else ''
            print(f'  {name}: {score:.2f} ({trade_counts.get(name, 0)} trades){tag}')

        # Fully swallowed, like the live/paper report below: a reporting bug must never
        # cost a monitoring cycle.
        try:
            DOMAIN.report_activity(performances, KEEP_TOP_N)
        except Exception as e:
            print(f'turnover report unavailable ({e})')

        # Who can still act. Swallowed for the same reason as every other reporter here.
        try:
            print_idle_report(performances, state, trade_counts, obs)
        except Exception as e:
            print(f'idle report unavailable ({e})')

        # What conditions the whole population is playing in, in one line.
        try:
            DOMAIN.report_regime(obs)
        except Exception as e:
            print(f'regime summary unavailable ({e})')

        # Whatever background writer the domain's per-tick signals depend on. Nothing
        # recorded any of this before, so no strategy could condition on it and no
        # post-mortem could reconstruct the conditions a result happened under.
        try:
            DOMAIN.ensure_background_jobs()
        except Exception as e:
            print(f'market recorder unavailable ({e})')

        # The gated-vs-control readout for whatever arms the domain is running, in the log
        # the next emperor pass reads. Net worth cannot answer that question in any
        # reasonable time -- it is one noisy sample an hour. Swallowed like every other
        # reporter here.
        try:
            DOMAIN.report_experiments()
        except Exception as e:
            print(f'basis edge report unavailable ({e})')

        current_leader, leader_score = performances[0]
        promote_live_strategy(current_leader, leader_score)
        live_state = load_live_strategy()
        live_name = live_state['name'] if live_state else None

        # One line per cycle on how the live strategy's real results compare to the paper
        # book it is ranked on. emperor_logs are the primary input to the next emperor
        # pass, and without this the divergence exists only in the strategy's own stdout
        # -- which is where 343 consecutive refused orders sat unnoticed on 2026-08-03.
        # Fully swallowed: a reporting bug must never cost a monitoring cycle.
        if live_name:
            try:
                DOMAIN.report_live(live_name)
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
        # Untrack the deep tail: stopped, never acted, nothing to learn from.
        # Files stay on disk; the live strategy and anything with a track record are
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
                name=fresh, source_repo=DOMAIN.TEMPLATE_REPO,
                parent_name=fresh, parent_cfg=None,
                score=DOMAIN.STARTING_SCORE, inject=True, seed_fallback=True,
                role=ROLE_EXPLORE,
            ))

        # Every newcomer gets revised now, so there is no target draw. When the budget is
        # short of the batch (a backfill cycle already spent some of it, or
        # REVISIONS_PER_CYCLE was lowered), explore goes first: it is the scarce role and
        # the only one that can introduce logic the population does not already run.
        # Everything not revised falls back to DOMAIN.tweak_config / DOMAIN.seed_config,
        # exactly as it always has when the model was unavailable.
        ordered = sorted(specs, key=lambda s: s['role'] != ROLE_EXPLORE)
        revising = {s['name'] for s in ordered[:budget]}
        print(f'Revision budget {budget}/{len(specs)}; revising '
              f'{", ".join(sorted(revising)) or "nothing"} this cycle '
              f'({", ".join(s["name"] + "/" + s["role"] for s in specs)})')
        # One newcomer must not be able to end the cycle. Provisioning runs a revision
        # model, git, and a smoke test over LLM-written code and LLM-written config, and
        # an uncaught exception anywhere in there used to propagate out of run() and kill
        # monitor outright -- emperor.sh then sits out the rest of its window with nothing
        # scoring, culling or spawning. A failed newcomer is skipped instead: it is not
        # started, so it stays a checked-out-but-never-started clone and the next cycle's
        # `strat_manager.py prune` drops it.
        provisioned = []
        for spec in specs:
            try:
                provision_strategy(**spec, leaderboard=leaderboard, obs=obs,
                                   revise=(spec['name'] in revising))
            except Exception as e:
                traceback.print_exc()
                print(f'Provisioning {spec["name"]} failed ({e}); skipping it this cycle')
                continue
            provisioned.append(spec)

        # Start the newcomers, plus any top-N strategy that's currently stopped
        # (reload state first since strat_manager.py clone/stop wrote to disk)
        for spec in provisioned:
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
  --ensure-recorder   start the domain's background jobs if they are not already
                      running, print their report, and exit.
                      Exits 1 if they could not be confirmed running.
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
            DOMAIN.ensure_background_jobs()
        except Exception as e:
            print(f'market recorder unavailable ({e})')
        alive = DOMAIN.background_jobs_alive()
        print(f'recorder running: {alive}')
        sys.exit(0 if alive else 1)
    else:
        print(USAGE, end='')
        sys.exit(0 if args in (['-h'], ['--help']) else 2)

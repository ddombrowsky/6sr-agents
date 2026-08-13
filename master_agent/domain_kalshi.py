#!/usr/bin/env python3
"""The Kalshi domain: real prediction markets, scored mark-to-market against the crowd.

See KALSHI.md for the full engineering plan this implements (Phase 1: money-free,
mirroring domain_forecast.py's shape). domain_forecast.py proved the generic loop can
improve a population on a domain that isn't sdex; this domain trades a synthetic,
instantly-resolving question generator for real Kalshi markets that take hours to days to
settle -- the "core design problem" KALSHI.md names, and the reason scoring here cannot
be a straight copy of either existing domain.

THE GAME
========
tools/kalshi_api.py is the keyless read layer (Horizon's role in domain_sdex.py).
tools/kalshi_recorder.py is a supervised single-writer daemon (like market_recorder.py)
that polls the open markets the current strategy population is actually forecasting on
and caches them, so N strategies on a tick loop don't turn into N times that many Kalshi
requests. A strategy's `decide(market, history, config)` reads one cached market
(kalshi_recorder.current_markets()) and returns p_hat; kalshi_recorder.submit_forecast()
logs that forecast alongside the market's CURRENT price, frozen at that moment.
tools/kalshi_reconcile.py is a second supervised daemon that finds forecasts whose market
has since settled and appends the real outcome. See kalshi_recorder.py's module
docstring for why scoring is split exactly this way.

WHY score() AND score_path() DIVERGE HERE (same shape as domain_forecast.py, worse cause)
================================================================================================
domain_forecast.py's score() reads a running tally the template keeps in state.json,
populated FROM forecast_engine.submit_forecast()'s synchronous return value -- the
outcome is known the instant a forecast is made, so main.py's own tick loop can tally it.
Here the outcome is NOT known at forecast time; it becomes known later, asynchronously,
in a SEPARATE process (kalshi_reconcile.py) that a strategy's own main() never talks to.
So main.py has nothing trustworthy to tally, and score() below is not a meaningful
fallback beyond STARTING_SCORE -- score_path() is the only real judge, reading
kalshi_recorder.cumulative_brier() off the append-only log directly, independent of
anything main.py claims about itself.

WHY THE MARK-TO-MARKET NULL CANNOT BE SCORED "INSTANTLY" -- read before changing this
============================================================================================
KALSHI.md's design section describes scoring p_hat against p_market "that instant." Taken
literally that is impossible for a genuine skill signal: ANY score built only from
(p_hat, p_market), with no outcome, is -- by the definition of a proper scoring rule --
uniquely optimized by reporting p_hat = p_market exactly. A formula built that way would
make PARROTING THE MARKET MATHEMATICALLY UNBEATABLE, not merely a good baseline, and
evolution would converge on pure copying, the opposite of this domain's purpose. What
actually implements KALSHI.md's intent, and what is built here: p_market is read and
FROZEN the moment a forecast is made (kalshi_recorder.submit_forecast), so the eventual
comparison is not biased by a market price that has since moved toward the answer -- but
the SCORE (Brier of p_hat vs. outcome, compared to Brier of the frozen p_market vs.
outcome) can only be computed once the market resolves, via kalshi_reconcile.py. This is
exactly "the market's implied probability" as FUTURE.md's stated null for this domain,
evaluated honestly instead of instantly. Do not "simplify" score_path() into a distance
between p_hat and p_market -- see the math above for why that would silently invert the
entire domain's purpose.

NO score_timeout_factor -- see KALSHI.md's "Lessons from domain_forecast.py's actual run"
================================================================================================
domain_forecast.py grew a self-revision-added decay (`score_timeout_factor`) that fights
Phase 2's promotion gate, which explicitly wants forecast count and age as a POSITIVE
signal. Deliberately not ported here, on purpose, per KALSHI.md's explicit instruction.

NO DAEMON WAS domain_forecast's SHORTCUT; THIS DOMAIN DOESN'T GET IT
==========================================================================
forecast_engine's question generation and resolution are pure functions of a seed, so
`ensure_background_jobs`/`background_jobs_alive` stay no-ops there. Kalshi's markets are
real external state on their own clock, so this domain supervises TWO daemons
(kalshi_recorder.py, kalshi_reconcile.py) and `background_jobs_alive()` can genuinely
return False.

STILL NO MONEY -- Phase 1 only
===================================
`caps()` is None and `can_execute_live()` is always False, same honest-always-false
pattern as domain_null.py and domain_forecast.py. Real execution
(tools/kalshi_trader.py, a KYC'd account, signed requests) is KALSHI.md's Phase 2, a
separate, later decision.

Select it with DOMAIN=kalshi.
"""
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import domain

NAME = 'kalshi'

# Overridable for the same reason domain_forecast.FORECAST_TEMPLATE_REPO is: exercised
# from a scratch directory by selftest_domain.py, and from a fresh container's own local
# template repo by whatever runs it for real.
TEMPLATE_REPO = os.environ.get('KALSHI_TEMPLATE_REPO', 'file:///opt/template_repo_kalshi')

STARTING_SCORE = 1000.0

# monitor.py's rank-KEEP_TOP_N cull used to exempt every domain for a flat 3h
# (YOUNG_GRACE_S) after a strategy starts acting -- long enough on domain_forecast, whose
# questions resolve every ~30s, but not here. KALSHI.md's "core design problem" section
# calls this out by name: an hourly, rank-based cull scored on markets that take days to
# resolve would stop strategies on a trade-count tiebreak, not real evidence, because
# score_path() sits at bare STARTING_SCORE until something resolves. Observed in Phase 1's
# first real run: a strategy that logged ~600 forecasts in 4h had only 25 resolved. 24h
# covers a full day of Climate/Weather dailies (Phase 1's starting category_filter) --
# same "not tuned to a specific run yet, revisit once Phase 1 has run longer" caveat as
# CONFIDENCE_PRIOR_N/CONFIDENCE_CAP above.
RANK_GRACE_S = 24 * 3600

# Each point of (mean_market_brier - mean_brier), summed over every RESOLVED forecast, is
# worth this many score points -- same summed-not-averaged shape as domain_forecast's
# SCORE_SCALE, for the same reason (criterion 5: more good calls should outrank fewer
# good calls at the same accuracy). Real Kalshi markets are efficiently priced, so
# per-forecast edges over the market will typically be much smaller than
# domain_forecast's synthetic ones -- SCORE_SCALE only sets the overall spread, ranking
# is what the loop actually uses, so this is not tuned to a specific run yet.
SCORE_SCALE = 1000.0

# domain_forecast.py's CONFIDENCE_PRIOR_N=200 assumed resolution every ~30s; a resolved
# COUNT of 200 there was a few hours. Kalshi markets settle over hours to days (Phase 1's
# starting filter is DAILY weather markets), so demanding 200 resolved forecasts before
# trusting a track record at all would leave every strategy shrunk to STARTING_SCORE for
# weeks. These are round numbers scaled down for that reason, not fit to a real run --
# revisit exactly like domain_forecast.py's own comment says to, once Phase 1 has run
# long enough to know the real resolved-forecast rate.
CONFIDENCE_PRIOR_N = 20.0
CONFIDENCE_CAP = 300.0

# No money exists in this domain yet (Phase 1). See domain.py's contract: an empty dict
# is the honest answer, unlike sdex where omitting PAPER_ONLY would let a smoke run place
# a real order.
SMOKE_ENV = {}

OBSERVE_FAILURE_NOTE = 'Could not reach the Kalshi API, or no open markets in DEFAULT_CATEGORY right now'

REPLAY_WINDOW = 'a fixed set of already-resolved Kalshi markets (see kalshi_backtest.N_MARKETS)'
# Not literally "days" -- kept positive and truthy for the same reason
# domain_forecast.REPLAY_DAYS is. The real window size lives in kalshi_backtest.N_MARKETS.
REPLAY_DAYS = 1

MAX_MARKETS_PER_TICK = 20
MAX_CONFIDENCE_GAIN = 3.0
DEFAULT_CATEGORY = 'Climate and Weather'

TRADES_DIR = domain.TRADES_DIR


def _strategy_python():
    """See domain_sdex.py's identical function: one definition of "which python runs a
    strategy," imported at CALL time so this module stays importable outside the
    container (strat_manager.py mkdirs /opt/strategies as an import side effect)."""
    from strat_manager import _strategy_python as resolve
    return resolve()


@dataclass
class Observation:
    """When this cycle's direct kalshi_api check ran, and how many open, liquid
    DEFAULT_CATEGORY markets it found -- a live proof-of-reachability, not a cache
    freshness reading (see observe()). Deliberately as thin as
    domain_forecast.Observation's tick counter."""
    ts: float = 0.0
    market_count: int = 0


# --------------------------------------------------------------------------------------
# Shared lazy handles on /opt/tools, mirroring domain_sdex.py's _tools() /
# domain_forecast.py's _forecast_engine() pattern: appended to sys.path lazily inside
# each function that needs it, never at module import time, so this module stays
# importable (for domain.check(), for selftest_domain.py run outside a container) even
# where /opt/tools does not exist or lacks network-facing dependencies like `requests`.
# --------------------------------------------------------------------------------------

def _kalshi_api():
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import kalshi_api
        return kalshi_api
    except Exception as e:
        print(f'kalshi_api unavailable ({e})')
        return None


def _kalshi_recorder():
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import kalshi_recorder
        return kalshi_recorder
    except Exception as e:
        print(f'kalshi_recorder unavailable ({e})')
        return None


def _kalshi_backtest():
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import kalshi_backtest
        return kalshi_backtest
    except Exception as e:
        print(f'kalshi_backtest unavailable ({e})')
        return None


def _known_categories():
    """kalshi_api.KNOWN_CATEGORIES, or None if kalshi_api can't be reached right now
    (no network dependency, host run, etc). Config validation below treats None as
    "cannot verify" and fails OPEN -- accepting whatever string is there -- rather than
    rejecting every config because a tooling outage made verification unavailable,
    same policy domain.py's contract requires of criterion 2's replay/importability."""
    api = _kalshi_api()
    return list(api.KNOWN_CATEGORIES) if api is not None else None


# --------------------------------------------------------------------------------------
# Criterion 1: fitness resolvable inside a cycle.
# --------------------------------------------------------------------------------------

def observe():
    """A direct, cheap kalshi_api read -- deliberately NOT kalshi_recorder's cached
    log. The recorder is a daemon ensure_background_jobs() may have JUST spawned this
    same cycle (cold start, or after a container restart): its first snapshot can take
    a real number of seconds once it exists at all, and gating observe() on it would
    make the very first cycle after a restart fail with 'no observation' every time,
    the same trap this would fall into if domain_sdex.observe() read market_recorder's
    log instead of calling price_feed.get_price() directly. kalshi_api itself has its
    own short cache (30s) and doesn't depend on the recorder daemon's timing at all."""
    api = _kalshi_api()
    if api is None:
        return None
    markets = api.list_open_markets(category=DEFAULT_CATEGORY, frequency='daily',
                                    max_series=3, limit_per_series=10)
    if not markets:
        return None
    return Observation(ts=time.time(), market_count=len(markets))


def observe_population(obs, state):
    """Nothing to enrich -- each strategy reads kalshi_recorder itself, the same way
    each domain_forecast strategy calls forecast_engine itself."""
    return obs


def encode_observation(obs):
    return json.dumps({'ts': obs.ts, 'market_count': obs.market_count})


def decode_observation(text):
    if not text:
        return None
    try:
        data = json.loads(text)
        return Observation(ts=float(data['ts']), market_count=int(data['market_count']))
    except Exception:
        return None


def observation_line(obs):
    if obs is None:
        return 'No observation for this cycle (Kalshi API unreachable, or nothing open).\n'
    return (f'Kalshi API reachable: {obs.market_count} open, liquid market(s) in '
            f'{DEFAULT_CATEGORY!r} right now.\n')


def _shrunk_edge(count, mean_brier, mean_market_brier):
    """Blend a strategy's observed mean_brier with the mean brier the market's OWN
    frozen-at-forecast-time price would have scored, weighted down for a small resolved
    sample (CONFIDENCE_PRIOR_N), then weight the resulting edge by volume up to a
    ceiling (CONFIDENCE_CAP). Direct structural copy of domain_forecast._shrunk_edge --
    see CONFIDENCE_PRIOR_N/CONFIDENCE_CAP above for why the magnitudes differ."""
    shrunk_brier = ((CONFIDENCE_PRIOR_N * mean_market_brier + count * mean_brier)
                     / (CONFIDENCE_PRIOR_N + count))
    weight = min(count, CONFIDENCE_CAP)
    return weight * (mean_market_brier - shrunk_brier)


def score(state_dict, obs):
    """See module docstring's WHY score() AND score_path() DIVERGE section: unlike
    domain_forecast, main.py's own tick loop never learns a forecast's outcome (that
    happens later, in kalshi_reconcile.py, a separate process), so there is no
    trustworthy running tally to read here. STARTING_SCORE is the honest answer;
    score_path() is the real judge, and (mirroring domain_forecast's own note)
    DOMAIN.score() is never called outside a test."""
    return STARTING_SCORE, []


def score_path(strategy_path, obs):
    """The authoritative score: independently judged from the forecast log via
    kalshi_recorder.cumulative_brier(), never from anything main.py claims about
    itself. None if the log can't be read (no forecasts logged yet is NOT the same as
    unreadable -- that case returns STARTING_SCORE, same distinction
    domain_forecast.score_path draws)."""
    rec = _kalshi_recorder()
    if rec is None:
        return None
    name = Path(strategy_path).name
    count, mean_brier, mean_market_brier = rec.cumulative_brier(name)
    if count == 0:
        return STARTING_SCORE
    return STARTING_SCORE + SCORE_SCALE * _shrunk_edge(count, mean_brier, mean_market_brier)


def activity_log_path(name):
    return TRADES_DIR / f'{name}.log'


def activity(name):
    """(count, first_ts, last_ts) of forecasts MADE (resolved or not) -- volume/recency,
    same shape domain_null.py and domain_forecast.py's activity() use. Deliberately
    counts 'forecast' rows only, not the 'resolution' rows kalshi_reconcile.py appends
    to the same file -- those are a different process's activity, not this strategy's."""
    log_path = activity_log_path(name)
    if not log_path.exists():
        return 0, 0.0, 0.0
    count = 0
    first = last = 0.0
    try:
        with log_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get('type') != 'forecast':
                    continue
                count += 1
                ts = row.get('timestamp', 0.0)
                if not first:
                    first = ts
                last = ts
    except Exception:
        pass
    return count, first, last


def config_signature(state_entry):
    try:
        cfg = json.load(open(Path(state_entry['path']) / 'config.json'))
    except Exception:
        return None
    return (cfg.get('confidence_gain'), cfg.get('markets_per_tick'), cfg.get('category_filter'))


# --------------------------------------------------------------------------------------
# Criterion 2: a cheap offline replay.
# --------------------------------------------------------------------------------------

def replay(strategy_dir):
    """Replay over kalshi_backtest.py's fixed, already-resolved market set, or None if
    unmeasurable. See domain_sdex.replay()/domain_forecast.replay() for why None (not
    False) on failure: this guards a fitness signal, not money, and failing closed on a
    tooling or network outage would revert every revision in the population at once."""
    bt = _kalshi_backtest()
    if bt is None:
        return None
    try:
        result = bt.replay(str(strategy_dir))
        return {'trades': int(result.get('trades', 0)),
                'beats_null': result.get('beats_null'),
                'null_pct': result.get('null_pct'),
                'raw': result}
    except Exception as e:
        print(f'  kalshi replay unavailable ({e}); not gating on it')
        return None


def importability(source_or_path):
    bt = _kalshi_backtest()
    if bt is None:
        return None
    try:
        return bt.importability_report(source_or_path)
    except Exception as e:
        print(f'importability check unavailable ({e}); not gating on it')
        return None


# --------------------------------------------------------------------------------------
# Criterion 4: caps enforced outside the agent's reach. No money exists yet (Phase 1).
# --------------------------------------------------------------------------------------

def caps():
    """No money, no caps. None is the contract's "cannot be consulted" value."""
    return None


def can_execute_live(name):
    """Fails closed like sdex's, but for a domain with no execution path yet the honest
    answer is that nothing can be promoted to real money. See KALSHI.md's Phase 2."""
    return False, 'the kalshi domain has no execution path yet (Phase 2, not built)'


def promotion_sizing(name):
    return None


def prepare_live(name):
    return {}


def retire_live(old_name):
    return True, []


# --------------------------------------------------------------------------------------
# Criterion 6: synthesize and reject genomes. Three knobs: markets_per_tick (volume),
# confidence_gain (how hard to lean into the template's default momentum/mean-reversion
# read -- see template_repo_kalshi/main.py's decide() docstring for why it is NOT a
# knob on copying the market price), and category_filter (the "genuinely new direction"
# mutation lever KALSHI.md names as this domain's second one, beyond magnitude tweaks).
# --------------------------------------------------------------------------------------

def normalize_config(cfg_path, name):
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return False
    if not isinstance(cfg, dict):
        return False
    filled = {}
    if 'name' not in cfg:
        filled['name'] = name
    if 'markets_per_tick' not in cfg:
        filled['markets_per_tick'] = 1
    if 'confidence_gain' not in cfg:
        filled['confidence_gain'] = 1.0
    if 'category_filter' not in cfg:
        filled['category_filter'] = DEFAULT_CATEGORY
    if not filled:
        return False
    cfg.update(filled)
    json.dump(cfg, open(cfg_path, 'w'), indent=2)
    print(f'  filled missing config keys for {name}: '
          f'{", ".join(f"{k}={v!r}" for k, v in filled.items())}')
    return True


def sanitize_config(cfg_path, obs=None):
    """Nothing to re-verify against a live venue the way sdex checks assets/trustlines
    for a rug -- a category_filter naming a category Kalshi doesn't have is a stale
    enum value, which repair_config below fixes; it is not evidence of an attack."""
    return []


def repair_config(cfg_path, name, obs):
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return []
    if not isinstance(cfg, dict):
        return []
    repairs = []
    try:
        gain = float(cfg.get('confidence_gain'))
    except (TypeError, ValueError):
        gain = None
    if gain is None or not -MAX_CONFIDENCE_GAIN <= gain <= MAX_CONFIDENCE_GAIN:
        old = cfg.get('confidence_gain')
        cfg['confidence_gain'] = 1.0
        repairs.append(f'confidence_gain {old!r} -> 1.0 '
                       f'(outside [-{MAX_CONFIDENCE_GAIN}, {MAX_CONFIDENCE_GAIN}])')
    try:
        mpt = int(cfg.get('markets_per_tick'))
    except (TypeError, ValueError):
        mpt = None
    if mpt is None or not 1 <= mpt <= MAX_MARKETS_PER_TICK:
        old = cfg.get('markets_per_tick')
        cfg['markets_per_tick'] = 1
        repairs.append(f'markets_per_tick {old!r} -> 1 (outside [1, {MAX_MARKETS_PER_TICK}])')
    category = cfg.get('category_filter')
    known = _known_categories()
    if not isinstance(category, str) or (known is not None and category not in known):
        old = category
        cfg['category_filter'] = DEFAULT_CATEGORY
        repairs.append(f'category_filter {old!r} -> {DEFAULT_CATEGORY!r} '
                       f'(not a known Kalshi category)')
    if not repairs:
        return []
    json.dump(cfg, open(cfg_path, 'w'), indent=2)
    for line in repairs:
        print(f'  repaired config for {name}: {line}')
    return repairs


def config_is_sane(cfg, name, obs):
    if not isinstance(cfg.get('name'), str) or cfg['name'] != name:
        return False
    try:
        gain_ok = -MAX_CONFIDENCE_GAIN <= float(cfg['confidence_gain']) <= MAX_CONFIDENCE_GAIN
        mpt_ok = 1 <= int(cfg['markets_per_tick']) <= MAX_MARKETS_PER_TICK
        cat_ok = isinstance(cfg.get('category_filter'), str) and bool(cfg['category_filter'])
        return gain_ok and mpt_ok and cat_ok
    except (KeyError, TypeError, ValueError):
        return False


def inject_experiments(cfg_path, obs):
    """The mechanical novelty channel: a coin flip at switching category_filter to a
    different known category -- KALSHI.md names this as the domain's "genuinely new
    direction" lever, distinct from domain_forecast's single magnitude-only knob."""
    import random
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return False
    known = _known_categories()
    if not known or random.random() >= 0.5:
        return False
    choices = [c for c in known if c != cfg.get('category_filter')]
    if not choices:
        return False
    cfg['category_filter'] = random.choice(choices)
    json.dump(cfg, open(cfg_path, 'w'), indent=2)
    print(f"  seeded category_filter={cfg['category_filter']!r}")
    return True


def seed_config(cfg_path, name, obs):
    import random
    existing = {}
    if Path(cfg_path).exists():
        try:
            loaded = json.load(open(cfg_path))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            pass
    known = _known_categories() or [DEFAULT_CATEGORY]
    new_cfg = dict(existing)
    new_cfg.update({'name': name,
                    'confidence_gain': round(random.uniform(-1.0, 1.0), 3),
                    'markets_per_tick': existing.get('markets_per_tick', 1),
                    'category_filter': existing.get('category_filter') or random.choice(known)})
    json.dump(new_cfg, open(cfg_path, 'w'), indent=2)


def tweak_config(parent_cfg_path, new_cfg_path, new_name):
    import random
    try:
        parent = json.load(open(parent_cfg_path))
    except Exception as e:
        print(f'  could not read parent config {parent_cfg_path}: {e}')
        return False
    if not isinstance(parent, dict):
        return False
    try:
        gain = float(parent['confidence_gain'])
    except (KeyError, TypeError, ValueError) as e:
        print(f'  parent config {parent_cfg_path} has no usable confidence_gain: {e}')
        return False
    new_cfg = dict(parent)
    new_gain = min(MAX_CONFIDENCE_GAIN, max(-MAX_CONFIDENCE_GAIN, gain * random.uniform(0.9, 1.1)))
    new_cfg.update({'name': new_name, 'confidence_gain': round(new_gain, 3)})
    json.dump(new_cfg, open(new_cfg_path, 'w'), indent=2)
    return True


def check_replayable(source, baseline_source, name):
    """Would kalshi_backtest still be able to see this main.py? (ok, reason), or None.
    Direct structural copy of domain_forecast.check_replayable -- see that function's
    docstring for the None-means-carry-on / only-gate-on-a-regression policy."""
    verdict = importability(source)
    if verdict is None or verdict[0]:
        return None
    why = verdict[1]
    baseline_ok = None
    if baseline_source is not None:
        baseline_verdict = importability(baseline_source)
        baseline_ok = baseline_verdict[0] if baseline_verdict else None
    if baseline_ok:
        return False, (f'kalshi_backtest cannot import decide() from main.py ({why}), '
                        f'so beats_null would describe the fallback rule rather than '
                        f'this code -- and the main.py it replaces was importable')
    print(f'NOTE: {name} is backtest-blind ({why}); the main.py it replaces was too, '
          f'so this is not treated as a regression')
    return None


def prepare_smoke_config(cfg, obs):
    """Nothing needs adjusting to make a config runnable -- unlike sdex, there is no
    possible incompatibility between a threshold band and a live price to repair."""
    return cfg


def check_smoke_state(raw, cfg, obs):
    """Is the state.json a smoke run wrote a legal one? (ok, detail). Requires at least
    one real forecast to have been logged, same non-numeric-field-isn't-enough
    reasoning as domain_forecast.check_smoke_state. Depends on kalshi_recorder already
    having live data cached (ensure_background_jobs runs every cycle, so this is
    normally true) -- an empty recorder log during a smoke test is a real, visible
    failure here rather than a false pass, which is the point."""
    try:
        made = int(raw.get('forecasts_made', 0) or 0)
    except (TypeError, ValueError):
        return False, 'main.py wrote a non-numeric forecasts_made value'
    if made <= 0:
        return False, 'main.py ran but never logged a forecast'
    return True, f'{made} forecast(s) made'


def cleanup_scratch(scratch_name):
    activity_log_path(scratch_name).unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# Criterion 5's instruments, and the two background writers they depend on.
# --------------------------------------------------------------------------------------

RECORDER_SCRIPT = Path('/opt/tools/kalshi_recorder.py')
RECONCILE_SCRIPT = Path('/opt/tools/kalshi_reconcile.py')
RECORDER_INTERVAL = 300
RECONCILE_INTERVAL = 300
RECORDER_PID_FILE = TRADES_DIR / '.kalshi_recorder.pid'
RECONCILE_PID_FILE = TRADES_DIR / '.kalshi_reconcile.pid'
RECORDER_LOG = TRADES_DIR / 'kalshi_recorder.log'
RECONCILE_LOG = TRADES_DIR / 'kalshi_reconcile.log'


def _pid_alive(pid_file, name_fragment):
    """Pid file plus two confirmations, same reasoning as domain_sdex.background_jobs_alive:
    a stale pid file surviving a container restart can name a live, unrelated process."""
    try:
        pid = int(pid_file.read_text().strip())
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
    return name_fragment in cmdline


def background_jobs_alive():
    """Both daemons must be up -- a live recorder feeding a dead reconciler would let
    forecasts pile up unresolved forever, and a live reconciler with no recorder has
    nothing fresh to forecast against in the first place."""
    return (_pid_alive(RECORDER_PID_FILE, 'kalshi_recorder')
            and _pid_alive(RECONCILE_PID_FILE, 'kalshi_reconcile'))


def _spawn(script, pid_file, log_file, interval):
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    log = open(log_file, 'a')
    proc = subprocess.Popen(
        [_strategy_python(), '-u', str(script), '--daemon', '--interval', str(interval)],
        stdout=log, stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,   # survives a TERM to monitor's process group
    )
    pid_file.write_text(str(proc.pid))
    return proc.pid


def ensure_background_jobs():
    """Start whichever of the two daemons isn't already running, then report on
    freshness. Idempotent and called once per cycle, same as domain_sdex's version --
    the common path here is background_jobs_alive() plus a status line, nothing more."""
    if not _pid_alive(RECORDER_PID_FILE, 'kalshi_recorder'):
        pid = _spawn(RECORDER_SCRIPT, RECORDER_PID_FILE, RECORDER_LOG, RECORDER_INTERVAL)
        print(f'Started kalshi_recorder (pid {pid}, every {RECORDER_INTERVAL}s) -> {RECORDER_LOG}')
    if not _pid_alive(RECONCILE_PID_FILE, 'kalshi_reconcile'):
        pid = _spawn(RECONCILE_SCRIPT, RECONCILE_PID_FILE, RECONCILE_LOG, RECONCILE_INTERVAL)
        print(f'Started kalshi_reconcile (pid {pid}, every {RECONCILE_INTERVAL}s) -> {RECONCILE_LOG}')

    rec = _kalshi_recorder()
    if rec is None:
        return
    span = rec.span()
    rows = rec.tail(1)
    last = rows[0] if rows else {}
    age = round(time.time() - last['ts']) if last.get('ts') else None
    print(f"Kalshi market history: {span['rows']} rows over {span['hours']}h; "
          f"last row {age}s ago")
    if age is not None and age > RECORDER_INTERVAL * 5:
        print(f'WARNING: kalshi market history is {age}s stale. Check {RECORDER_LOG}')


def report_activity(performances, limit=None):
    rec = _kalshi_recorder()
    if rec is None:
        return
    rows = []
    for name, _score in performances[:limit]:
        made = rec.forecasts_made(name)
        if not made:
            continue
        count, mean_brier, mean_market_brier = rec.cumulative_brier(name)
        rows.append((name, made, count, mean_brier, mean_market_brier))
    if not rows:
        return
    print('Kalshi forecasts (resolved mean Brier vs. the market it saw, lower is better):')
    for name, made, count, mean_brier, mean_market_brier in rows:
        if count:
            print(f'  {name}: {made} forecast(s), {count} resolved, '
                  f'brier={mean_brier:.4f} vs market={mean_market_brier:.4f}')
        else:
            print(f'  {name}: {made} forecast(s), none resolved yet')


def stuck_report(performances, state, obs):
    return None


def report_regime(obs):
    pass


def report_experiments():
    pass


def report_live(live_name):
    pass


def prompt_facts():
    known = _known_categories() or [DEFAULT_CATEGORY]
    return {
        'starting_score': STARTING_SCORE,
        'score_scale': SCORE_SCALE,
        'confidence_prior_n': CONFIDENCE_PRIOR_N,
        'confidence_cap': CONFIDENCE_CAP,
        'max_markets_per_tick': MAX_MARKETS_PER_TICK,
        'max_confidence_gain': MAX_CONFIDENCE_GAIN,
        'known_categories': known,
        'null_baseline': (
            'the market\'s own implied probability (last traded price), frozen at the '
            'moment a forecast is made -- a strategy that always reports p_hat equal to '
            'that frozen price scores EXACTLY the market\'s own resolved Brier, no '
            'better and no worse; score_path only rewards a p_hat that ends up, once '
            'the market settles, genuinely closer to the true outcome than the price '
            'the strategy could have traded at was'
        ),
    }

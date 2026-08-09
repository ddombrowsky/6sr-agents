#!/usr/bin/env python3
"""The forecasting-benchmark domain: FUTURE.md item 3's free, fast, no-money benchmark.

domain_null.py's docstring names this module before it existed: "It is also the skeleton
for FUTURE.md's item 3... A real benchmark domain replaces observe()/score()/replay()
with resolved questions and a Brier score against the base rate, and keeps everything
else." This is that replacement -- a new module, not domain_null.py edited in place,
because selftest_domain.py's differential tests pin domain_null.py's exact (deliberately
fake) behavior as the minimal contract-conformance reference; this one is meant to be
real instead.

THE GAME
========
See tools/forecast_engine.py for the actual mechanics (question generation, resolution,
scoring) -- this module is the thin domain-contract wrapper around it, the same
relationship domain_sdex.py has to price_feed.py/trade_logger.py/backtest.py. In short: a
strategy is handed a `feature` per question (a noisy signal correlated with the hidden
true probability) and must answer with a probability `p_hat`; forecast_engine scores it
against the outcome via the Brier score, independently of whatever main.py claims about
itself -- unlike domain_null's `score`, which just trusts a number the strategy wrote to
its own state.json.

WHY score() AND score_path() DIVERGE HERE
============================================
domain.py's contract has both `score(state_dict, obs)` and `score_path(dir, obs)`.
monitor.py's live loop only ever calls the latter (compute_strategy_score ->
DOMAIN.score_path(state_entry['path'], obs)) -- `score()` exists for parity and for
selftest_domain.py's differential-style checks, which is also true for domain_sdex.py
(grep the codebase: DOMAIN.score(...) is never called outside a test). For sdex that
distinction is invisible because a state.json's balances are the whole story either way.
Here it is not: this domain's real judge is the append-only forecast log
(tools/forecast_engine.py's cumulative_brier), keyed by strategy NAME, and `score()` only
ever receives a bare state dict with no name attached. So:

  * `score_path()` is authoritative -- it derives the name from the path and reads
    forecast_engine.cumulative_brier(name) off the log directly. A strategy's own code
    cannot inflate this number; it never sees the outcome, only `feature`.
  * `score()` reads a redundant running tally (`forecasts_made`/`brier_sum`/
    `base_brier_sum`) that the template's main() loop keeps in state.json purely so this
    fallback has something to read. That tally is maintained FROM the values
    forecast_engine.submit_forecast() returns (never computed independently by main.py),
    so it is exactly as trustworthy as sdex's balance_usd/balance_xlm -- convention-
    enforced, not cryptographically so. A sufficiently adversarial revision could in
    principle write to it directly; that is the same threat model sdex already accepts
    (see forecast_engine.py's TRUST MODEL note), not a new gap this domain introduces.

NO DAEMON, NO MONEY
====================
`ensure_background_jobs`/`background_jobs_alive` stay no-ops, same as domain_null.py:
forecast_engine's question generation and resolution are both pure functions of a fixed
seed, so there is nothing to supervise. `caps()` is None and `can_execute_live()` is
always False, same honest-always-false pattern as domain_null.py -- there is no money in
this domain and nothing here should pretend otherwise.

Select it with DOMAIN=forecast.
"""
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import domain

NAME = 'forecast'

# Overridable for the same reason domain_null's NULL_TEMPLATE_REPO is: this domain is
# exercised from a scratch directory by selftest_domain.py, and from a fresh container's
# own local template repo by whatever runs it for real. There is no /opt/template_repo
# for a forecasting game -- that path is sdex's.
TEMPLATE_REPO = os.environ.get('FORECAST_TEMPLATE_REPO', 'file:///opt/template_repo_forecast')

STARTING_SCORE = 1000.0
# Each point of (base_brier - brier), summed over every resolved forecast, is worth this
# many score points. Summed rather than averaged so volume compounds -- more good calls
# ranks higher than fewer good calls at the same accuracy, which is this domain's direct
# behavioral answer to FUTURE.md's criterion 5 (an hourly cull over too few independent
# bets is mostly sampling noise). Kept as a named constant, read live by prompt_facts(),
# rather than a literal in master-agent.py's forecast prompt -- see that module's
# docstring on why a number stated in a prompt and a number enforced in code drifting
# apart is a recurring, specifically-named failure mode in this codebase.
SCORE_SCALE = 1000.0

# How much weight a strategy's own track record gets before it is trusted over the base
# rate, and the ceiling on how much raw volume alone can grow a score once it clearly is
# trusted. Both serve the SAME purpose SCORE_SCALE's comment names -- a lucky small sample
# must not be able to outrank an established track record -- they just asymptote instead
# of compounding forever. Measured against the 2026-08-06 forecast run's cycle-5
# leaderboard: uncapped, a 3210-forecast strategy at brier=0.2199 permanently outranked a
# 1854-forecast strategy at a genuinely better brier=0.2128, and every strategy spawned
# after cycle 1 was still short of the leader's forecast count five cycles (7+ hours)
# later -- score was rewarding age, not skill, once both strategies had long since cleared
# any reasonable noise floor. CONFIDENCE_PRIOR_N sets that floor (below it, score stays
# close to STARTING_SCORE regardless of how lucky the sample looks); CONFIDENCE_CAP sets
# where volume stops mattering and skill takes over completely. Both are round numbers,
# not fit to this one run -- revisit with a real backtest if questions_per_tick or
# TICK_SECONDS changes enough to move how many forecasts a cycle actually produces.
CONFIDENCE_PRIOR_N = 200.0
CONFIDENCE_CAP = 1500.0  # about 12.5 hours

# No money exists in this domain, so there is no execution to suppress. See domain.py's
# contract: an empty dict is the honest answer here, unlike sdex where omitting
# PAPER_ONLY would let a smoke run place a real order.
SMOKE_ENV = {}

OBSERVE_FAILURE_NOTE = 'Could not read the question generator'

REPLAY_WINDOW = 'a fixed set of pre-generated resolved questions (see forecast_backtest.N_QUESTIONS)'
# Not literally "days" here -- kept positive and truthy because REPLAY_DAYS is still read
# as a number by report/prompt code that formats it as "Nd". The real window size lives
# in forecast_backtest.N_QUESTIONS, not here, so the two can't drift independently.
REPLAY_DAYS = 1


@dataclass
class Observation:
    """A tick counter -- there is no market to observe, only which tick's questions are
    live right now. See forecast_engine.current_tick()."""
    tick: int = 0


# --------------------------------------------------------------------------------------
# Shared lazy handles on /opt/tools, mirroring domain_sdex.py's _tools() pattern: appended
# to sys.path lazily inside each function that needs it, never at module import time, so
# domain_forecast.py stays importable (for domain.check(), for selftest_domain.py run
# outside a container) even where /opt/tools does not exist.
# --------------------------------------------------------------------------------------

def _forecast_engine():
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import forecast_engine
        return forecast_engine
    except Exception as e:
        print(f'forecast_engine unavailable ({e})')
        return None


def _forecast_backtest():
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import forecast_backtest
        return forecast_backtest
    except Exception as e:
        print(f'forecast_backtest unavailable ({e})')
        return None


# --------------------------------------------------------------------------------------
# Criterion 1: fitness resolvable inside a cycle.
# --------------------------------------------------------------------------------------

def observe():
    engine = _forecast_engine()
    if engine is None:
        return None
    return Observation(tick=engine.current_tick())


def observe_population(obs, state):
    """Nothing to enrich -- each strategy calls forecast_engine itself, the same way
    each sdex strategy calls price_feed.get_price() itself; there is no batched
    per-population network call to make here (there is no network at all)."""
    return obs


def encode_observation(obs):
    return json.dumps({'tick': obs.tick})


def decode_observation(text):
    if not text:
        return None
    try:
        return Observation(tick=int(json.loads(text)['tick']))
    except Exception:
        return None


def observation_line(obs):
    if obs is None:
        return 'No observation for this cycle.\n'
    return (f'Cycle tick: {obs.tick}. There is no price in this domain -- only questions '
            f'and resolved outcomes.\n')


def _shrunk_edge(count, mean_brier, mean_base):
    """Blend a strategy's observed mean_brier with the base rate it's measured against,
    weighted down for a small sample (CONFIDENCE_PRIOR_N), then weight the resulting edge
    by volume up to a ceiling (CONFIDENCE_CAP) rather than without bound. See those
    constants for why: this is what stops score from just measuring strategy age."""
    shrunk_brier = ((CONFIDENCE_PRIOR_N * mean_base + count * mean_brier)
                     / (CONFIDENCE_PRIOR_N + count))
    # this should reach full weight in about 12.5 hours
    weight = min(count, CONFIDENCE_CAP)
    return weight * (mean_base - shrunk_brier)


def score(state_dict, obs):
    """See the module docstring's WHY score() AND score_path() DIVERGE section. Reads
    the running tally the template keeps in state.json, not the log."""
    try:
        count = float(state_dict.get('forecasts_made', 0) or 0)
        brier_sum = float(state_dict.get('brier_sum', 0.0) or 0.0)
        base_sum = float(state_dict.get('base_brier_sum', 0.0) or 0.0)
    except (TypeError, ValueError):
        return STARTING_SCORE, ['forecasts_made']
    if count <= 0:
        return STARTING_SCORE, []
    edge = _shrunk_edge(count, brier_sum / count, base_sum / count)
    return STARTING_SCORE + SCORE_SCALE * edge, []

def score_timeout_factor(count):
    """After a period of time, the score begins to degrade back to 0.  This allows
    a perfect strategy to rise to the top, and then leave room for new clones.
    This is a test domain, after all.  It can be useful for testing new templates
    or LLMs against each other."""
    t0 = 24 * 3600 # no score change in first 24 hours
    t1 = 72 * 3600 # score goes to 0 after 72 hours
    sec_per_count = 30
    t = sec_per_count * count
    if (t <= t0): return 1
    if (t >= t1): return 0
    x = (t - t0) / (t1 - t0)
    return 0.5 * (1.0 + math.cos(math.pi * x))

def score_path(strategy_path, obs):
    """The authoritative score: independently judged from the forecast log, not from
    whatever main.py claims about itself. None if the log can't be read (no forecasts
    logged yet is NOT the same as unreadable -- that case returns STARTING_SCORE)."""
    engine = _forecast_engine()
    if engine is None:
        return None
    name = Path(strategy_path).name
    count, mean_brier, mean_base = engine.cumulative_brier(name)
    if count == 0:
        return STARTING_SCORE
    # mathematically, this means the score will approach 376000 if the strategy
    # is perfectly accurate (i.e. mean_brier = 0).  base_brier -- in this test forecast
    # module -- is always 0.25, so the mean is always 0.25.
    return ((STARTING_SCORE +
             SCORE_SCALE *
             _shrunk_edge(count, mean_brier, mean_base)) *
            score_timeout_factor(count))


def activity_log_path(name):
    return domain.TRADES_DIR / f'{name}.log'


def activity(name):
    """(count, first_ts, last_ts) of resolved forecasts -- same JSON-lines shape
    domain_null.py and forecast_engine.py both use, so this is a straight port."""
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
                count += 1
                try:
                    ts = json.loads(line).get('timestamp', 0.0)
                except Exception:
                    continue
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
    return (cfg.get('confidence_gain'), cfg.get('questions_per_tick'))


# --------------------------------------------------------------------------------------
# Criterion 2: a cheap offline replay.
# --------------------------------------------------------------------------------------

def replay(strategy_dir):
    """Replay over forecast_backtest.py's fixed historical question set, or None if
    unmeasurable. See domain_sdex.replay() for why None (not False) on failure: this
    guards a fitness signal, not money, and failing closed on a tooling outage would
    revert every revision in the population at once."""
    bt = _forecast_backtest()
    if bt is None:
        return None
    try:
        result = bt.replay(str(strategy_dir))
        return {'trades': int(result.get('trades', 0)),
                'beats_null': result.get('beats_null'),
                'null_pct': result.get('null_pct'),
                'raw': result}
    except Exception as e:
        print(f'  forecast replay unavailable ({e}); not gating on it')
        return None


def importability(source_or_path):
    bt = _forecast_backtest()
    if bt is None:
        return None
    try:
        return bt.importability_report(source_or_path)
    except Exception as e:
        print(f'importability check unavailable ({e}); not gating on it')
        return None


# --------------------------------------------------------------------------------------
# Criterion 4: caps enforced outside the agent's reach. No money exists in this domain.
# --------------------------------------------------------------------------------------

def caps():
    """No money, no caps. None is the contract's "cannot be consulted" value."""
    return None


def can_execute_live(name):
    """Fails closed like sdex's, but for a domain with nothing to execute the honest
    answer is that no strategy can ever be promoted to real money."""
    return False, 'the forecast domain has no execution path, so nothing can go live'


def promotion_sizing(name):
    return None


def prepare_live(name):
    return {}


def retire_live(old_name):
    return True, []


# --------------------------------------------------------------------------------------
# Criterion 6: synthesize and reject genomes. Two knobs: questions_per_tick (volume) and
# confidence_gain (calibration -- p_hat = clip(0.5 + gain * (feature - 0.5), 0, 1) in the
# template's default decide()).
# --------------------------------------------------------------------------------------

MAX_QUESTIONS_PER_TICK = 20
MAX_CONFIDENCE_GAIN = 3.0


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
    if 'questions_per_tick' not in cfg:
        filled['questions_per_tick'] = 1
    if 'confidence_gain' not in cfg:
        filled['confidence_gain'] = 1.0
    if not filled:
        return False
    cfg.update(filled)
    json.dump(cfg, open(cfg_path, 'w'), indent=2)
    print(f'  filled missing config keys for {name}: '
          f'{", ".join(f"{k}={v!r}" for k, v in filled.items())}')
    return True


def sanitize_config(cfg_path, obs=None):
    """Nothing to re-verify: there are no third-party assets or venues to be rugged."""
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
    if gain is None or not 0.0 <= gain <= MAX_CONFIDENCE_GAIN:
        old = cfg.get('confidence_gain')
        cfg['confidence_gain'] = 1.0
        repairs.append(f'confidence_gain {old!r} -> 1.0 (outside [0.0, {MAX_CONFIDENCE_GAIN}])')
    try:
        qpt = int(cfg.get('questions_per_tick'))
    except (TypeError, ValueError):
        qpt = None
    if qpt is None or not 1 <= qpt <= MAX_QUESTIONS_PER_TICK:
        old = cfg.get('questions_per_tick')
        cfg['questions_per_tick'] = 1
        repairs.append(f'questions_per_tick {old!r} -> 1 (outside [1, {MAX_QUESTIONS_PER_TICK}])')
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
        gain_ok = 0.0 <= float(cfg['confidence_gain']) <= MAX_CONFIDENCE_GAIN
        qpt_ok = 1 <= int(cfg['questions_per_tick']) <= MAX_QUESTIONS_PER_TICK
        return gain_ok and qpt_ok
    except (KeyError, TypeError, ValueError):
        return False


def inject_experiments(cfg_path, obs):
    """The one mechanical novelty channel: a coin flip at asking more questions per
    tick. Mirrors domain_null.py's inject_experiments exactly -- there is only one
    genuinely new direction to try mechanically until a second knob earns its keep."""
    import random
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return False
    if cfg.get('questions_per_tick', 1) != 1 or random.random() >= 0.5:
        return False
    cfg['questions_per_tick'] = 2
    json.dump(cfg, open(cfg_path, 'w'), indent=2)
    print('  seeded questions_per_tick=2')
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
    new_cfg = dict(existing)
    new_cfg.update({'name': name,
                    'confidence_gain': round(random.uniform(0.5, 1.5), 3),
                    'questions_per_tick': existing.get('questions_per_tick', 1)})
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
    new_gain = min(MAX_CONFIDENCE_GAIN, max(0.0, gain * random.uniform(0.9, 1.1)))
    new_cfg.update({'name': new_name, 'confidence_gain': round(new_gain, 3)})
    json.dump(new_cfg, open(new_cfg_path, 'w'), indent=2)
    return True


def check_replayable(source, baseline_source, name):
    """Would forecast_backtest still be able to see this main.py? (ok, reason), or None.

    None means "no verdict, carry on" for every internal failure -- see importability():
    this guards a fitness signal, and failing closed on a tooling outage would revert
    every revision in the population at once. Only gates (returns False) when the
    candidate is blind AND the main.py it would replace was not -- a revision that was
    already blind and stays blind is kept, since reverting it would throw away whatever
    else it changed for no gain in visibility. Same policy as domain_sdex.py's, without
    the strict/lenient env toggle -- not worth the extra knob for a first cut.
    """
    verdict = importability(source)
    if verdict is None or verdict[0]:
        return None
    why = verdict[1]
    baseline_ok = None
    if baseline_source is not None:
        baseline_verdict = importability(baseline_source)
        baseline_ok = baseline_verdict[0] if baseline_verdict else None
    if baseline_ok:
        return False, (f'forecast_backtest cannot import decide() from main.py ({why}), '
                        f'so beats_null would describe the fallback rule rather than '
                        f'this code -- and the main.py it replaces was importable')
    print(f'NOTE: {name} is backtest-blind ({why}); the main.py it replaces was too, '
          f'so this is not treated as a regression')
    return None


def prepare_smoke_config(cfg, obs):
    """Nothing needs adjusting to make a config runnable here -- unlike sdex, there is
    no possible incompatibility between a threshold band and a live price to repair."""
    return cfg


def check_smoke_state(raw, cfg, obs):
    """Is the state.json a 120s smoke run wrote a legal one? (ok, detail).

    Requires at least one real forecast to have been logged, not just a numeric field --
    at TICK_SECONDS=30 a healthy loop gets several ticks inside SMOKE_TEST_SECONDS
    (120s default), so `forecasts_made == 0` means the loop started but never actually
    called forecast_engine, which domain_null.py's mere "is points numeric" check would
    have missed entirely.
    """
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
# Criterion 5's instruments.
# --------------------------------------------------------------------------------------

def report_activity(performances, limit=None):
    engine = _forecast_engine()
    if engine is None:
        return
    rows = []
    for name, _score in performances[:limit]:
        count, mean_brier, mean_base = engine.cumulative_brier(name)
        if count:
            rows.append((name, count, mean_brier, mean_base))
    if not rows:
        return
    print('Forecasts logged (mean Brier vs. base, lower is better):')
    for name, count, mean_brier, mean_base in rows:
        print(f'  {name}: {count} resolved, brier={mean_brier:.4f} vs base={mean_base:.4f}')


def stuck_report(performances, state, obs):
    return None


def report_regime(obs):
    pass


def report_experiments():
    pass


def report_live(live_name):
    pass


def ensure_background_jobs():
    """Nothing to supervise -- forecast_engine's question generation and resolution are
    both pure functions of a fixed seed. See the module docstring."""
    pass


def background_jobs_alive():
    return True


def prompt_facts():
    engine = _forecast_engine()
    noise_sd = engine.NOISE_SD if engine is not None else 0.15
    base_brier = engine.BASE_BRIER if engine is not None else 0.25
    return {
        'starting_score': STARTING_SCORE,
        'score_scale': SCORE_SCALE,
        'base_brier': base_brier,
        'feature_noise_sd': noise_sd,
        'max_questions_per_tick': MAX_QUESTIONS_PER_TICK,
        'max_confidence_gain': MAX_CONFIDENCE_GAIN,
        'null_baseline': (
            'a strategy that ignores the visible feature and always guesses 0.5 scores '
            f'exactly the base Brier score above ({base_brier}), no better than chance; '
            'confidence_gain=1.0 applied to (feature - 0.5) is well-calibrated at the '
            'live game\'s noise level'
        ),
    }

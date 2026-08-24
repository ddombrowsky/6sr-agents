#!/usr/bin/env python3
"""The reference domain: a coin-flip forecasting game with no prices and no money.

This exists to prove that domain.py's contract carries a domain that has none of the
things the Stellar DEX has -- no price, no order book, no assets, no issuers, no
threshold band, no spread, no trustlines, no real-money caps -- and that monitor.py can
therefore score, gate, seed, mutate, cull and promote against it unchanged. Reading the
loop is not enough to establish that; running it is. selftest_domain.py drives
main_py_is_sane and provision_strategy against this module for exactly that reason.

It is also the skeleton for FUTURE.md's item 3 (a free, fast, unambiguously-scored
benchmark domain, in a fresh container). What is deliberately NOT here is a real game:
`score` reads a number a strategy wrote down, and nothing checks whether the strategy
earned it. A real benchmark domain replaces observe()/score()/replay() with resolved
questions and a Brier score against the base rate, and keeps everything else.

The game, such as it is: a strategy writes `{"points": N}` into state.json. Fitness is
STARTING_SCORE + points. The null is "always guess the base rate", which scores exactly
STARTING_SCORE -- so beats_null is `points > 0`. There is no execution, so there is no
money boundary to enforce and caps() is None.

Select it with DOMAIN=null.
"""
import json
import os
import random
from dataclasses import dataclass
from pathlib import Path

import domain

NAME = 'null'

# Overridable because this domain is used from a scratch directory by the self-test, and
# from a fresh container by whatever succeeds it. There is no /opt/template_repo for a
# forecasting game.
TEMPLATE_REPO = os.environ.get('NULL_TEMPLATE_REPO', 'file:///opt/template_repo_null')

STARTING_SCORE = 1000.0

# How fast this domain wants to be RUN. create.sh writes these into v/env.sh (see
# domain.py's group 8), so `./create.sh null` brings up a container whose monitor cycles
# every 2 minutes and whose emperor window is 5 minutes, instead of the image's 8h/12h.
#
# The point of this domain is to exercise the loop, and the loop's unit of progress is a
# cycle: score, rank, cull, clone, revise, gate. At the sdex cadence a full turnover of
# the population takes most of a day, which is the right speed when each observation is a
# real market and the wrong speed when the whole game resolves in 5-second ticks and
# costs nothing. Two minutes is comfortably longer than one cycle's own work (the
# SMOKE_TEST_SECONDS=120 smoke run is the slow part, and cycles that overrun simply
# arrive late -- monitor sleeps between cycles, it does not schedule them).
#
# EMPEROR_RUN_HOURS is a *window*, not a period: emperor.sh runs monitor.py for it, then
# asks it to stop and spends however long the self-revision LLM call takes before the
# next window opens. So five minutes is the floor of the emperor cycle, not its length.
# It is deliberately more than one CYCLE_SLEEP: a window shorter than a cycle would stop
# monitor at its first sleep every time and the emperor would review a log of one cycle.
#
# IDLE_GRACE_S is monitor.py's, not this module's, and is scaled for the same reason
# RANK_GRACE_S below is: at 3h it is 90 cycles here, so a strategy whose main.py died
# would go on being ranked for three hours of a test that is meant to show turnover.
#
# EMPEROR_LOG_RETENTION is here because emperor.sh prunes to a count of cycles, not a span
# of time: its default 48 is 24 days at the sdex window and under four hours at this one,
# on the domain whose entire job is to leave a readable record of what the loop did. 288
# is a day's worth at five minutes.
PACING = {
    'CYCLE_SLEEP': '120',
    'EMPEROR_RUN_HOURS': '5m',
    'IDLE_GRACE_S': '360',
    'EMPEROR_LOG_RETENTION': '288',
}

# See domain_sdex.RANK_GRACE_S's comment: 3h is the value every domain shared as
# monitor.py's bare YOUNG_GRACE_S constant before it became domain-owned. Ticks resolve
# instantly here and PACING above runs cycles 30x faster than monitor.py's own 3600s
# default, so this is the same *three cycles* that constant always meant -- expressed against this domain's cadence rather
# than sdex's. Left at 3h it would exempt every newcomer from the rank cull for 90
# cycles, which is a population that never turns over: exactly the behaviour this domain
# exists to test.
RANK_GRACE_S = 3 * 120

# No money exists in this domain, so there is no execution to suppress. An empty dict is
# the honest answer and the contract explicitly allows it -- unlike sdex, where omitting
# PAPER_ONLY would let a smoke run place a real order.
SMOKE_ENV = {}

OBSERVE_FAILURE_NOTE = 'Could not read the question queue'

# How many resolved questions a replay covers. Free and instant, which is the whole
# argument for a domain like this one: sdex's replay needs 720 candles off a rate-limited
# exchange API, and criterion 2 is the gate everything else depends on.
REPLAY_DAYS = 1
REPLAY_WINDOW = 'the resolved question set'


@dataclass
class Observation:
    """A cycle counter. There is no market to observe, and that is the point."""
    tick: int = 0


def observe():
    return Observation(tick=int(random.random() * 0))    # deterministic: always 0


def observe_population(obs, state):
    """Nothing to enrich -- no per-population network I/O exists here."""
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
    return f'Cycle tick: {obs.tick}. There is no price in this domain.\n'


def _points(strategy_path):
    try:
        data = json.load(open(Path(strategy_path) / 'state.json'))
    except Exception:
        return None
    try:
        return float(data.get('points', 0.0))
    except (TypeError, ValueError):
        return None


def score(state_dict, obs):
    try:
        return STARTING_SCORE + float(state_dict.get('points', 0.0)), []
    except (TypeError, ValueError):
        return STARTING_SCORE, ['points']


def score_path(strategy_path, obs):
    points = _points(strategy_path)
    return None if points is None else STARTING_SCORE + points


def activity_log_path(name):
    return domain.TRADES_DIR / f'{name}.log'


def activity(name):
    """(count, first_ts, last_ts) of resolved forecasts, from the same one-JSON-per-line
    log shape sdex uses -- monitor's idle detection and promotion gates read this."""
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
    return (cfg.get('confidence'), cfg.get('questions_per_tick'))


def replay(strategy_dir):
    """A replay that costs nothing: count how many forecasts the config would produce.

    Returns None only when the config is unreadable, so the loop's fail-open path is
    exercised by the self-test rather than being theoretical.
    """
    try:
        cfg = json.load(open(Path(strategy_dir) / 'config.json'))
    except Exception:
        return None
    per_tick = int(cfg.get('questions_per_tick') or 0)
    return {'trades': per_tick * 10, 'beats_null': per_tick > 0, 'null_pct': 0.0,
            'raw': {'questions_per_tick': per_tick}}


def importability(source_or_path):
    """No replay engine imports the genome here, so there is nothing to check."""
    return None


def live_enabled():
    """(enabled, reason). Constant False, for the same reason can_execute_live is.

    There is no money in this domain, so the only honest answer is that real
    execution is off and cannot be switched on.
    The switch is still a real member rather than an inherited default: the loop asks it
    every cycle, and a domain that answered "enabled" here while refusing every strategy
    below would put a "live trading is on" line in the cycle log of a system with no
    money in it.
    """
    return False, 'the null domain has no execution path, so nothing can ever be live'


def caps():
    """No money, no caps. None is the contract's "cannot be consulted" value and every
    caller in the loop already tolerates it."""
    return None


def can_execute_live(name):
    """Fails closed like sdex's, but for a domain with nothing to execute the honest
    answer is that no strategy can ever be promoted to real money."""
    return False, 'the null domain has no execution path, so nothing can go live'


def promotion_sizing(name):
    return None


def prepare_live(name):
    return {}


def retire_live(old_name):
    return True, []


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
    if not filled:
        return False
    cfg.update(filled)
    json.dump(cfg, open(cfg_path, 'w'), indent=2)
    print(f'  filled missing config keys for {name}: '
          f'{", ".join(f"{k}={v!r}" for k, v in filled.items())}')
    return True


def sanitize_config(cfg_path, obs=None):
    """Nothing to re-verify: there are no third-party assets to be rugged."""
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
        confidence = float(cfg.get('confidence'))
    except (TypeError, ValueError):
        confidence = None
    if confidence is None or not 0.5 <= confidence <= 1.0:
        # `old` captured before the assignment, or the message reports the repaired value
        # as though it were the defect -- which is how you get a log line reading
        # "confidence 0.6 -> 0.6".
        old = cfg.get('confidence')
        cfg['confidence'] = 0.6
        repairs.append(f'confidence {old!r} -> 0.6 (outside [0.5, 1.0])')
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
        return 0.5 <= float(cfg['confidence']) <= 1.0 and int(cfg['questions_per_tick']) > 0
    except (KeyError, TypeError, ValueError):
        return False


def inject_experiments(cfg_path, obs):
    """The one mechanical novelty channel: a coin flip at answering more per tick."""
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
                    'confidence': round(random.uniform(0.55, 0.9), 3),
                    'questions_per_tick': existing.get('questions_per_tick', 1)})
    json.dump(new_cfg, open(cfg_path, 'w'), indent=2)


def tweak_config(parent_cfg_path, new_cfg_path, new_name):
    try:
        parent = json.load(open(parent_cfg_path))
    except Exception as e:
        print(f'  could not read parent config {parent_cfg_path}: {e}')
        return False
    if not isinstance(parent, dict):
        return False
    try:
        confidence = float(parent['confidence'])
    except (KeyError, TypeError, ValueError) as e:
        print(f'  parent config {parent_cfg_path} has no usable confidence: {e}')
        return False
    new_cfg = dict(parent)
    new_cfg.update({'name': new_name,
                    'confidence': round(min(1.0, max(0.5, confidence * random.uniform(0.95, 1.05))), 3)})
    json.dump(new_cfg, open(new_cfg_path, 'w'), indent=2)
    return True


def check_replayable(source, baseline_source, name):
    return None


def prepare_smoke_config(cfg, obs):
    return cfg


def check_smoke_state(raw, cfg, obs):
    try:
        points = float(raw.get('points', 0.0))
    except (TypeError, ValueError):
        return False, 'main.py wrote a non-numeric points value'
    return True, f'{points:+.1f} points'


def cleanup_scratch(scratch_name):
    activity_log_path(scratch_name).unlink(missing_ok=True)


def report_activity(performances, limit=None):
    rows = [(name, activity(name)[0]) for name, _ in performances[:limit]]
    rows = [(name, count) for name, count in rows if count]
    if not rows:
        return
    print('Forecasts logged:')
    for name, count in rows:
        print(f'  {name}: {count} resolved')


def stuck_report(performances, state, obs):
    return None


def report_regime(obs):
    pass


def report_experiments():
    pass


def report_live(live_name):
    pass


def ensure_background_jobs():
    pass


def background_jobs_alive():
    """Nothing to supervise, so nothing can be down. True keeps monitor.py's
    --ensure-recorder exit status meaningful for a domain with no daemon."""
    return True


def prompt_facts():
    return {'starting_score': STARTING_SCORE, 'null_baseline': 'always guess the base rate'}

#!/usr/bin/env python3
"""Yield rotation across Blend's lending pools. A STUB, and deliberately a small one.

YIELD.md's "what to do, in order" puts `domain_yield.py` at step 4, after the two
measurements that decide whether the domain is worth building at all. This module exists
before those measurements so the container can be brought up and the loop exercised
against a real venue set -- not because the question is settled. Everything here is
paper. Nothing can be promoted. Read the four limits below before treating any number it
produces as evidence:

  1. ~~The score is self-reported.~~ **Fixed.** `score_path` now recomputes the return
     from tools/yield_replay.py: the strategy's logged allocations, priced against
     tools/yield_recorder.py's rate history, charged the domain's cost model, and
     credited only for the time the recorder observed the process alive. `apy_bp` in
     state.json is ignored. What is ranked is EXCESS over a contemporaneous null, not
     yield, because every strategy here collects roughly the base rate and ranking on the
     total sorts the population mostly by beta.
  2. ~~There is no replay.~~ **Fixed, with a caveat.** `replay()` runs the candidate's
     own `choose()` over the recorded history via tools/yield_backtest.py. The caveat is
     that the history only goes back as far as the recorder has been running -- there is
     no archive behind it, so on a fresh container the gate is silent for the first six
     hours and short-windowed for the first few days. YIELD.md step 2's backfill is still
     the thing that would make it deep.
  3. **Blend only.** Aquarius is in YIELD.md and not in this module. Its pools cannot be
     entered single-sided, so an allocation there is an LP position carrying impermanent
     loss plus two book crossings, and nothing in this system prices either yet
     (YIELD.md section 3). Putting those venues in front of the population before that
     arithmetic exists would offer it an advertised rate that is not a return.
  4. **Live execution is off and cannot be switched on.** `live_enabled()` and
     `can_execute_live()` are constant False. The reason is YIELD.md section 2: a
     supplied position can be temporarily un-withdrawable when a pool runs out of free
     liquidity, and the existing machinery would read that as unsellable notional and
     print LIVE TRADING HALTED. Designing those stuck-semantics is step 4's largest
     piece of work and it is not done. A domain that moves money before it can say what
     "stuck" means is one halt away from a manual recovery.

WHAT IS REAL HERE. The venue set is live, not fictional: `observe()` refreshes
tools/yield_venues.py's snapshot, which reads the Blend v2 reward zone off chain, filters
to the pools that will actually accept a supply (`require_action_allowed` refuses one at
status 4 or 5 -- two of the six were frozen when this was written), and computes each
reserve's supply APY from the pool's own interest curve. On 2026-08-21 that was 20
allocatable reserves across four pools, and USDC alone paid 4.72% on YieldBlox, 6.59% on
Fixed and 1.67% + 2.98% of BLND emissions on Etherfuse. That dispersion in a single asset
is the thing worth searching, and it is reachable without touching Aquarius, without
impermanent loss and without selling an emission token.

THE GENOME encodes the document's open questions as knobs rather than as prose, so the
loop can put numbers on them:

    min_edge_bp             how much better a venue must look before the rotation is
                            worth its cost. The hurdle YIELD.md's measurement 1 is about.
    rebalance_hours         minimum hold. Rotation decisions arrive on a scale of days.
    max_venues              concentrate or spread.
    emission_weight         0..1, how much of the BLND emission APR to believe when
                            ranking. This is section 1's asymmetric friction made
                            searchable: emissions arrive in a token that must be sold
                            into its own book, so counting them at face value (1.0)
                            overstates the return and ignoring them (0.0) understates it.
                            The right answer is neither and nobody knows it yet.
    min_free_liquidity_usd  refuse a venue that could not be withdrawn from at size.
                            Section 2's concern, applied before allocating rather than
                            after halting.

Select it with DOMAIN=yield.
"""
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import domain

# /opt/tools is where the venue reader lives in the container. monitor.py already puts it
# on sys.path before importing a domain, but selftest and ad-hoc runs do not always.
for _tools in ('/opt/tools', str(Path(__file__).resolve().parent.parent / 'tools')):
    if _tools not in sys.path and Path(_tools).is_dir():
        sys.path.append(_tools)

NAME = 'yield'

TEMPLATE_REPO = os.environ.get('YIELD_TEMPLATE_REPO', 'file:///opt/template_repo_yield')

STARTING_SCORE = 1000.0

# Score is STARTING_SCORE + annualized EXCESS over the null, in basis points: a strategy
# beating the null by 1%/yr shows 1100, one matching it shows 1000, one that sat flat
# while the null earned shows below it. Annualized rather than cumulative on purpose --
# a cumulative total makes score a function of age, every number climbs forever, and
# rank-culling ends up measuring birthdays (the trap domain_null's template describes).

# YIELD.md's horizon section: rotation decisions arrive on a scale of days, not seconds,
# so a grace period sized for a domain whose feedback resolves in 30s would cull this one
# on the action-count tiebreak rather than on evidence. Three days is the document's own
# figure. Overridable because three days is also an unusable feedback loop for anyone
# testing the loop itself -- set YIELD_RANK_GRACE_S=3600 for that, and know that what you
# are then watching is noise.
RANK_GRACE_S = int(os.environ.get('YIELD_RANK_GRACE_S', 3 * 24 * 3600))

# The window both sides are measured over. Tied to RANK_GRACE_S deliberately: a score
# computed over a window longer than the loop waits before culling would rank strategies
# on a number that has not finished forming.
SCORE_WINDOW_S = RANK_GRACE_S

# Below this much covered history a strategy scores exactly STARTING_SCORE. Annualizing
# an hour of luck multiplies it by 8,760, and a newborn that happened to catch one good
# interval would out-rank everything that has actually been running -- which is the
# failure YOUNG_GRACE_S exists to prevent, arriving through the score instead of the cull.
#
# DERIVED from the window rather than fixed, and the 2026-08-22 shakedown is why. At the
# production window this is 6h, exactly what it was as a literal. But that run set
# YIELD_RANK_GRACE_S=1800 to make the loop turn over quickly, which shortened the window
# to 30 minutes while this stayed at 6h -- so nothing could be scored, every strategy sat
# at exactly STARTING_SCORE, and twenty of them were ranked and culled purely on
# monitor.py's trade-count tiebreak. That is the churn-rewarding failure this domain is
# built to avoid, reached by shortening one constant and not the other.
MIN_SCORING_S = SCORE_WINDOW_S // 12

# One bad sample -- a venue misreported at 400% for one interval -- must not be able to
# produce an unrankable number. +/-200%/yr is far outside anything real here and still
# leaves every plausible result untouched.
SCORE_CLAMP_BP = 20000.0

# Only used to state the cost amplification in prompt_facts. The scoring itself takes the
# constant from yield_replay so there is one definition of a year in the arithmetic.
SECONDS_PER_YEAR_HINT = 31536000.0

# No execution path exists (see limit 4), so a smoke run cannot place an order. PAPER_ONLY
# is set anyway: it costs nothing, tools/stellar_trader.py already honours it, and the day
# somebody adds an execution path to a strategy here they will do it in main.py without
# thinking about this constant.
SMOKE_ENV = {'PAPER_ONLY': '1'}

OBSERVE_FAILURE_NOTE = ('Could not read Blend pool rates (no fresh snapshot and the '
                        'chain read failed)')

# What the replay covers: however much rate history the recorder has, up to this. It is
# not a fixed archive like the other domains have -- the window grows as the container
# runs, and below yield_backtest.MIN_HISTORY_S there is no replay at all.
REPLAY_DAYS = 7
REPLAY_WINDOW = 'the recorded rate history'

# A snapshot older than this is not shown to the population. See yield_venues.read_snapshot:
# stale reads as absent, because an hour-old APY presented as current is the quiet failure.
# Sized against the recorder's 300s cadence, not against CYCLE_SLEEP: the daemon is what
# refreshes it, and a snapshot that has missed four samples means the daemon is in
# trouble, whether or not a monitor cycle happens to be due.
SNAPSHOT_MAX_AGE_S = 1800

RECORDER_SCRIPT = Path('/opt/tools/yield_recorder.py')
RECORDER_INTERVAL = 300
RECORDER_PID_FILE = domain.TRADES_DIR / '.yield_recorder.pid'
RECORDER_LOG = domain.TRADES_DIR / 'yield_recorder.log'


def _strategy_python():
    """Whichever interpreter runs a strategy, so a supervised daemon can never run under
    a different one. Imported at call time: strat_manager mkdirs /opt/strategies as an
    import side effect, which would otherwise make this module container-only."""
    from strat_manager import _strategy_python as resolve
    return resolve()


def _venues():
    """tools/yield_venues, or None if it cannot be imported. Never raises.

    Import failure is a normal state on a host without /opt/tools, and it must degrade to
    "no observation" rather than taking down the whole cycle at monitor's import time.
    """
    try:
        import yield_venues
        return yield_venues
    except Exception:
        return None


@dataclass
class Observation:
    """The allocatable venue set for one cycle, flattened to (pool, asset) rows."""
    as_of: float = 0.0
    rows: list = field(default_factory=list)

    def by_key(self):
        return {(row['pool_address'], row['asset_address']): row for row in self.rows}

    def best(self, emission_weight=0.0):
        if not self.rows:
            return None
        return max(self.rows, key=lambda r: r['supply_apy']
                   + emission_weight * r['emission_apr_gross'])


def observe():
    """The allocatable venue rows, from the snapshot the recorder daemon maintains.

    Reads rather than censuses. A census is ~21s of RPC and the daemon is already doing
    one every RECORDER_INTERVAL, so fetching a second one here would cost a cycle's
    latency to produce a slightly different view of the same moment -- and the scorer
    would then be pricing decisions against rates nobody was shown.

    Censusing directly is the fallback, not the path: it only happens when no daemon has
    ever run, which is the first cycle of a fresh container. A failed refresh falls back
    to the last snapshot at any age, because a stale venue list is still a better basis
    for one cycle than none -- and the age travels with it so observation_line can say so.
    """
    venues = _venues()
    if venues is None:
        return None
    payload = venues.read_snapshot(max_age_s=SNAPSHOT_MAX_AGE_S)
    if payload is None:
        payload = venues.write_snapshot() or venues.read_snapshot(max_age_s=None)
    if payload is None:
        return None
    rows = venues.allocatable_reserves(payload)
    if not rows:
        return None
    return Observation(as_of=float(payload.get('as_of', time.time())), rows=rows)


def observe_population(obs, state):
    """Nothing per-population to add: every strategy sees the same venue set, and what
    differs between them is what they do with it."""
    return obs


def encode_observation(obs):
    return json.dumps({'as_of': obs.as_of, 'rows': obs.rows})


def decode_observation(text):
    if not text:
        return None
    try:
        data = json.loads(text)
        return Observation(as_of=float(data.get('as_of', 0.0)),
                           rows=list(data.get('rows') or []))
    except Exception:
        return None


def observation_line(obs):
    if obs is None:
        return 'No venue observation this cycle.\n'
    age_min = max(0.0, (time.time() - obs.as_of) / 60.0)
    best = obs.best()
    worst = min(obs.rows, key=lambda r: r['supply_apy']) if obs.rows else None
    if best is None:
        return 'Venue snapshot is empty.\n'
    spread_bp = (best['supply_apy'] - worst['supply_apy']) * 10000
    return (f"{len(obs.rows)} allocatable Blend reserves, snapshot {age_min:.0f}m old. "
            f"Best base supply APY: {best['pool']}/{best['asset']} at "
            f"{best['supply_apy'] * 100:.2f}%; spread to the worst is {spread_bp:.0f}bp. "
            f"Emission APRs are gross of selling BLND.\n")


# ------------------------------------------------------------------------- scoring

def _state_of(strategy_path):
    try:
        return json.load(open(Path(strategy_path) / 'state.json'))
    except Exception:
        return None


def _replay():
    try:
        import yield_replay
        return yield_replay
    except Exception:
        return None


# score_path is called once per strategy per cycle and every call needs the same window of
# rate history and the same null. Loading and re-nulling per strategy would be the whole
# file times the population; this caches both for the life of one scoring pass, keyed on
# the history file's size and mtime so an appended sample invalidates it.
_SCORE_CACHE = {'stamp': None, 'window': None, 'history': [], 'nulls': {}}


def _history_stamp():
    try:
        import yield_recorder
        stat = yield_recorder.HISTORY_PATH.stat()
        return (stat.st_size, stat.st_mtime)
    except Exception:
        return None


def _window_history(now):
    """Rate-history rows covering the scoring window, cached for this pass."""
    stamp = _history_stamp()
    window = int(now // 60)          # re-null at most once a minute
    if _SCORE_CACHE['stamp'] != stamp or _SCORE_CACHE['window'] != window:
        try:
            import yield_recorder
            rows = yield_recorder.history(since=now - SCORE_WINDOW_S - 3600)
        except Exception:
            rows = []
        _SCORE_CACHE.update({'stamp': stamp, 'window': window,
                             'history': rows, 'nulls': {}})
    return _SCORE_CACHE['history']


def _null_for(history, since, until):
    """The benchmark for one span, cached: identical spans share one computation."""
    key = (round(since), round(until))
    if key not in _SCORE_CACHE['nulls']:
        replay = _replay()
        _SCORE_CACHE['nulls'][key] = (
            replay.null_static_best(history, since, until) if replay else None)
    return _SCORE_CACHE['nulls'][key]


def _scored(name, strategy_path, now=None):
    """The full scoring result for one strategy, or None if it cannot be measured.

    Both sides are measured over the SAME span, which is what makes the comparison mean
    anything: a strategy younger than the window is scored against a null that started
    when it did, rather than against one that had a three-day head start.
    """
    replay = _replay()
    if replay is None:
        return None
    now = now or time.time()
    history = _window_history(now)
    if not history:
        return None

    events = replay.load_events(activity_log_path(name), until=now)
    if not events:
        return None
    first_event = float(events[0]['timestamp'])
    since = max(now - SCORE_WINDOW_S, first_event, float(history[0]['ts']))
    if now - since < MIN_SCORING_S:
        return None

    mine = replay.run(history, replay.intent_changes(events), since, now, name=name)
    if not mine or mine['covered_s'] < MIN_SCORING_S:
        return None
    null = _null_for(history, since, now)
    if not null:
        return None

    excess = mine['return'] - null['return']
    annualized_bp = excess * (replay.SECONDS_PER_YEAR / mine['covered_s']) * 10000.0
    annualized_bp = max(-SCORE_CLAMP_BP, min(SCORE_CLAMP_BP, annualized_bp))
    return {'excess_bp': annualized_bp, 'mine': mine, 'null': null}


def score(state_dict, obs):
    """The state-dict form of the contract member. score_path is the authoritative one.

    A state dict does not identify a strategy -- the log to replay is found by name, and
    the only name that cannot be forged is the directory's. This form therefore trusts
    `state_dict['name']`, which normalize_config keeps equal to the directory name, and
    is here because the contract asks for it. monitor.py ranks with score_path.

    The second element is the contract's list of things that could not be priced: any
    venue the strategy claims to hold that is not in this cycle's allocatable set, which
    is how a strategy parked in a pool that has since frozen becomes visible.
    """
    unpriced = []
    if obs is not None:
        known = obs.by_key()
        for leg in state_dict.get('allocation') or []:
            key = (leg.get('pool_address'), leg.get('asset_address'))
            if key not in known:
                unpriced.append(f"{leg.get('pool')}/{leg.get('asset')}")
    name = state_dict.get('name')
    if not isinstance(name, str) or not name:
        return STARTING_SCORE, unpriced
    result = _scored(name, domain.STRATEGIES_DIR / name)
    if result is None:
        return STARTING_SCORE, unpriced
    return STARTING_SCORE + result['excess_bp'], unpriced


def score_path(strategy_path, obs):
    """STARTING_SCORE + annualized excess over the null, recomputed from evidence.

    Nothing here reads a number the strategy wrote. The allocations come from its log,
    the rates from the recorder, the costs from yield_replay's constants, and the time it
    was actually running from the recorder's pid observations.

    Returns STARTING_SCORE -- not None -- when a strategy cannot be measured yet. None
    means "error reading state" to monitor and sorts to -inf, which would cull every
    newborn in the population on its first cycle.
    """
    path = Path(strategy_path)
    if not path.exists():
        return None
    result = _scored(path.name, path)
    if result is None:
        return STARTING_SCORE
    return STARTING_SCORE + result['excess_bp']


def activity_log_path(name):
    return domain.TRADES_DIR / f'{name}.log'


def activity(name):
    """(count, first_ts, last_ts) of allocation events, one JSON object per line.

    The initial allocation counts as an action, which matters more here than in a trading
    domain: YIELD.md's horizon section notes that `is_idle` demotes anything that has
    never logged one, and in a yield domain the correct behaviour is frequently to hold.
    A strategy that allocated once and then sat still for three days is doing its job,
    not idling, and it must not read as idle to the loop.
    """
    log_path = activity_log_path(name)
    if not log_path.exists():
        return 0, 0.0, 0.0
    count = 0
    first = last = 0.0
    try:
        with log_path.open() as handle:
            for line in handle:
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
    """What makes two strategies the same strategy here: the whole allocation policy.

    Not the current allocation -- that is an output, and two configs that happen to hold
    the same venue this hour are still different policies.
    """
    try:
        cfg = json.load(open(Path(state_entry['path']) / 'config.json'))
    except Exception:
        return None
    return (cfg.get('min_edge_bp'), cfg.get('rebalance_hours'), cfg.get('max_venues'),
            cfg.get('emission_weight'), cfg.get('min_free_liquidity_usd'))


def _backtest():
    try:
        import yield_backtest
        return yield_backtest
    except Exception:
        return None


def replay(strategy_dir):
    """Run the candidate's own choose() over the recorded history. None if unmeasurable.

    None -- not False -- on every failure, including "the recorder has not collected
    enough history yet", which on a fresh container is the normal state for the first
    several hours. This guards a fitness signal rather than money, and failing closed on
    a tooling outage would revert every revision in the population at once.

    `trades` counts allocation decisions INCLUDING THE FIRST, not rotations. monitor.py
    discards a revision that replays zero trades, and in this domain never rotating is
    frequently the right answer -- counting rotations would revert the null for being
    the null. See tools/yield_backtest.py.
    """
    bt = _backtest()
    if bt is None:
        return None
    try:
        return bt.replay(str(strategy_dir))
    except Exception as e:
        print(f'  yield replay unavailable ({e}); not gating on it')
        return None


def importability(source_or_path):
    """(ok, reason) for whether choose() survives being imported out of main.py.

    Now that the backtester imports the genome, the template's "module top level holds
    only definitions" rule has a checker behind it instead of only a docstring.
    """
    bt = _backtest()
    if bt is None:
        return None
    try:
        return bt.importability_report(source_or_path)
    except Exception as e:
        print(f'importability check unavailable ({e}); not gating on it')
        return None


# ------------------------------------------------------------------- money boundary

_NO_LIVE = ('the yield domain is paper-only: withdrawal semantics for an illiquid '
            'lending position are not designed yet (YIELD.md section 2)')


def live_enabled():
    """(enabled, reason). Constant False, and not because the switch is off.

    Deferring to domain.live_switch() first would imply that clearing the operator's
    switch could turn this on. It cannot: this domain has no execution path at all, and
    the honest answer does not depend on the switch.
    """
    return False, _NO_LIVE


def caps():
    return None


def can_execute_live(name):
    return False, _NO_LIVE


def promotion_sizing(name):
    return None


def prepare_live(name):
    return {}


def retire_live(old_name):
    return True, []


# ---------------------------------------------------------------------- the genome

DEFAULTS = {
    'min_edge_bp': 50.0,
    'rebalance_hours': 12.0,
    'max_venues': 1,
    'emission_weight': 0.5,
    'min_free_liquidity_usd': 100.0,
}

BOUNDS = {
    'min_edge_bp': (0.0, 1000.0),
    'rebalance_hours': (0.25, 336.0),
    'max_venues': (1, 6),
    'emission_weight': (0.0, 1.0),
    'min_free_liquidity_usd': (0.0, 10_000_000.0),
}


def _clamp(key, value):
    low, high = BOUNDS[key]
    return type(low)(min(high, max(low, value)))


def _numeric(cfg, key):
    try:
        value = float(cfg[key])
    except (KeyError, TypeError, ValueError):
        return None
    if key == 'max_venues':
        value = int(value)
    low, high = BOUNDS[key]
    return value if low <= value <= high else None


def normalize_config(cfg_path, name):
    """Fill only what the loop can know: the name, and any knob missing entirely."""
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return False
    if not isinstance(cfg, dict):
        return False
    filled = {}
    if cfg.get('name') != name:
        filled['name'] = name
    for key, default in DEFAULTS.items():
        if key not in cfg:
            filled[key] = default
    if not filled:
        return False
    cfg.update(filled)
    json.dump(cfg, open(cfg_path, 'w'), indent=2)
    print(f'  filled missing config keys for {name}: '
          f'{", ".join(f"{k}={v!r}" for k, v in filled.items())}')
    return True


def sanitize_config(cfg_path, obs=None):
    """Drop venue preferences the world no longer offers.

    `venues` is an optional whitelist of "pool/asset" strings a revision may write to
    concentrate on particular reserves. A pool can freeze between cycles and a reserve can
    be disabled, at which point a whitelist naming only that venue would leave the
    strategy with nowhere to allocate and no way to say why -- so entries that are not in
    the current allocatable set are removed here, loudly, the same way sdex drops an asset
    whose issuer went away.
    """
    if obs is None or not getattr(obs, 'rows', None):
        return []
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return []
    wanted = cfg.get('venues')
    if not isinstance(wanted, list) or not wanted:
        return []
    live = {f"{row['pool']}/{row['asset']}" for row in obs.rows}
    kept = [v for v in wanted if v in live]
    dropped = [v for v in wanted if v not in live]
    if not dropped:
        return []
    cfg['venues'] = kept
    json.dump(cfg, open(cfg_path, 'w'), indent=2)
    return [f'dropped venue {v} (not allocatable this cycle)' for v in dropped]


def repair_config(cfg_path, name, obs):
    """Pull every knob back inside its bounds. One line per repair, as the loop expects."""
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return []
    if not isinstance(cfg, dict):
        return []
    repairs = []
    for key, default in DEFAULTS.items():
        if _numeric(cfg, key) is None:
            old = cfg.get(key)
            cfg[key] = default
            repairs.append(f'{key} {old!r} -> {default!r} (outside {BOUNDS[key]})')
    if not repairs:
        return []
    json.dump(cfg, open(cfg_path, 'w'), indent=2)
    for line in repairs:
        print(f'  repaired config for {name}: {line}')
    return repairs


def config_is_sane(cfg, name, obs):
    """All-or-nothing. A config that names the wrong strategy is not repairable here --
    it means the file belongs to something else."""
    if not isinstance(cfg.get('name'), str) or cfg['name'] != name:
        return False
    return all(_numeric(cfg, key) is not None for key in DEFAULTS)


def inject_experiments(cfg_path, obs):
    """The mechanical novelty channel: split across two venues instead of one.

    Deliberately the one knob a hill-climb from a single-venue parent will not find on
    its own -- `tweak_config`'s jitter moves thresholds, not structure, and every seeded
    strategy starts concentrated.
    """
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return False
    if int(cfg.get('max_venues', 1)) != 1 or random.random() >= 0.35:
        return False
    cfg['max_venues'] = 2
    json.dump(cfg, open(cfg_path, 'w'), indent=2)
    print('  seeded max_venues=2')
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
    new_cfg.update({
        'name': name,
        'schema_version': 1,
        # Spread across the plausible range rather than jittering one point: nobody knows
        # where the rotation hurdle sits, and seeding every strategy near the default
        # would hand the population one hypothesis to hill-climb from.
        'min_edge_bp': round(random.uniform(10.0, 300.0), 1),
        'rebalance_hours': round(random.choice([2.0, 6.0, 12.0, 24.0, 48.0]), 2),
        'max_venues': int(existing.get('max_venues', 1)),
        'emission_weight': round(random.uniform(0.0, 1.0), 3),
        'min_free_liquidity_usd': round(random.choice([0.0, 100.0, 1000.0, 10000.0]), 2),
    })
    json.dump(new_cfg, open(cfg_path, 'w'), indent=2)


def tweak_config(parent_cfg_path, new_cfg_path, new_name):
    try:
        parent = json.load(open(parent_cfg_path))
    except Exception as e:
        print(f'  could not read parent config {parent_cfg_path}: {e}')
        return False
    if not isinstance(parent, dict):
        return False
    if any(_numeric(parent, key) is None for key in ('min_edge_bp', 'rebalance_hours')):
        print(f'  parent config {parent_cfg_path} has no usable rotation policy')
        return False
    new_cfg = dict(parent)
    new_cfg['name'] = new_name
    for key in ('min_edge_bp', 'rebalance_hours', 'emission_weight',
                'min_free_liquidity_usd'):
        value = _numeric(parent, key)
        if value is None:
            continue
        new_cfg[key] = round(_clamp(key, value * random.uniform(0.95, 1.05)), 3)
    json.dump(new_cfg, open(new_cfg_path, 'w'), indent=2)
    return True


def check_replayable(source, baseline_source, name):
    """None: there is no replay to be excluded from. See replay()."""
    return None


def prepare_smoke_config(cfg, obs):
    """Make the smoke run act inside its few seconds.

    A candidate genome with rebalance_hours=48 would allocate once and then look
    identical to one that had crashed, because the harness only watches for a readable
    state.json. Zeroing the hold time and the hurdle means the smoke run has to reach the
    allocation code path to write anything at all.
    """
    cfg = dict(cfg)
    cfg['rebalance_hours'] = 0.0
    cfg['min_edge_bp'] = 0.0
    return cfg


def check_smoke_state(raw, cfg, obs):
    """Did the candidate allocate, and did it log that in the shape the scorer reads?

    The second half matters more than it looks. Scoring replays the activity log, not
    state.json, so a revision that renames `pool_address` or drops `weight` produces a log
    the scorer resolves to nothing -- and a strategy that resolves to nothing is scored as
    though it sat in cash for its entire life, which is a worse result than any allocation
    it could have made and one it has no way to see. The revision prompt says so in
    capitals; this is what makes saying so unnecessary. Breaking the log contract now
    reverts the revision instead of silently killing it.

    Fails OPEN if yield_replay cannot be imported: this guards a fitness signal, not
    money, and a tooling outage must not revert every revision in the population at once.
    """
    try:
        nav = float(raw.get('nav'))
    except (TypeError, ValueError):
        return False, 'main.py wrote no numeric nav'
    legs = raw.get('allocation')
    if not isinstance(legs, list):
        return False, 'main.py wrote no allocation list'
    # NOT `if not legs: fail`. The harness ends the run with SIGTERM, and a strategy
    # handles SIGTERM by flattening -- recording that it is out of the market and clearing
    # its own allocation before it exits. So an empty allocation here is what a CORRECTLY
    # shutting-down strategy leaves behind, and requiring a non-empty one failed the
    # unmodified template against its own smoke test. Both of the first two LLM revisions
    # on 2026-08-21 were discarded by that check, one of them for perfectly good code.
    #
    # The log below is the right place to ask the question anyway: it is what the scorer
    # reads, it survives the shutdown, and it records what the strategy did rather than
    # what it happened to be holding at the instant it was killed.
    detail = f'nav {nav:.6f}'

    replay = _replay()
    scratch = (cfg or {}).get('name')
    if replay is None or not scratch:
        return True, detail + ' (log contract unchecked)'

    events = replay.load_events(activity_log_path(scratch))
    if not events:
        return False, ('main.py ran but logged nothing to '
                       f'{activity_log_path(scratch)} -- the scorer reads that log, not '
                       'state.json, so this strategy would score as if it held cash')
    changes = [alloc for _ts, alloc in replay.intent_changes(events) if alloc]
    if not changes:
        return False, ('main.py logged events the scorer cannot resolve to a venue: an '
                       'allocate/rotate entry needs an `allocation` list whose legs each '
                       'carry pool_address, asset_address and weight')
    venues = {key for _ts, alloc in replay.intent_changes(events) for key in alloc}
    return True, (detail + f', {len(changes)} resolvable allocation event(s) '
                  f'across {len(venues)} venue(s)')


def cleanup_scratch(scratch_name):
    activity_log_path(scratch_name).unlink(missing_ok=True)


# --------------------------------------------------------------------- instruments

def report_activity(performances, limit=None):
    """Where each score came from: rotations, what they cost, and time spent flat.

    The turnover-vs-edge question in this domain's units, and the place a strategy that
    is losing to the null shows WHY. The three columns are the three ways to lose here --
    rotate too much, rotate across assets, or be stopped while the null keeps earning --
    and the score alone does not distinguish them.

    Note the amplification in the cost column: a one-off cost annualized over a three-day
    window is multiplied by ~122, so 1bp paid reads as ~122bp of score.
    """
    rows = []
    for name, _ in performances[:limit]:
        result = _scored(name, Path(domain.STRATEGIES_DIR) / name)
        if result is None:
            continue
        mine = result['mine']
        rows.append((name, result['excess_bp'], mine['rotations'], mine['cost_bp'],
                     mine['flat_s'] / 3600.0, mine['covered_s'] / 3600.0))
    if not rows:
        print('No strategy has enough recorded history to be scored yet.')
        return
    print('Score attribution (excess over the static null):')
    for name, excess_bp, rotations, cost_bp, flat_h, covered_h in rows:
        print(f'  {name}: {excess_bp:+.0f}bp excess over {covered_h:.0f}h, '
              f'{rotations} rotation(s) costing {cost_bp:.1f}bp, {flat_h:.1f}h flat')


def stuck_report(performances, state, obs):
    """Allocations that could not be withdrawn right now, in full, at today's liquidity.

    This is the instrument for YIELD.md section 2, and it is why it exists before any
    money does: the distinction between "illiquid by design, will free up as utilization
    falls" and "trapped" has to be visible in a paper run before it is load-bearing in a
    live one. A reserve at or above its max utilization is the first case; the report
    names it without deciding it is the second.
    """
    if obs is None:
        return None
    known = obs.by_key()
    lines = []
    for name, _ in performances:
        strategy_state = _state_of(Path(domain.STRATEGIES_DIR) / name)
        if not strategy_state:
            continue
        for leg in strategy_state.get('allocation') or []:
            row = known.get((leg.get('pool_address'), leg.get('asset_address')))
            if row is None:
                lines.append(f'  {name}: {leg.get("pool")}/{leg.get("asset")} '
                             f'is no longer allocatable')
                continue
            if row['utilization'] >= row['max_utilization']:
                lines.append(f'  {name}: {row["pool"]}/{row["asset"]} at '
                             f'{row["utilization"] * 100:.1f}% utilization '
                             f'(max {row["max_utilization"] * 100:.1f}%) -- '
                             f'withdrawal would fail until someone repays')
    if not lines:
        return None
    return 'Illiquid allocations:\n' + '\n'.join(lines)


def report_regime(obs):
    """This cycle's venues, and the answer to YIELD.md's kill criterion so far.

    The second block is measurement 2, recomputed every cycle off the recorded history:
    what the best possible rotation path was worth against simply sitting in the venue
    that turned out best. It is the number that decides whether this domain should exist,
    and printing it every cycle means nobody has to remember to go and ask.
    """
    if obs is None or not obs.rows:
        return
    ranked = sorted(obs.rows, key=lambda r: -(r['supply_apy'] + r['emission_apr_gross']))
    print('Venues (base APY + gross emission APR, best first):')
    for row in ranked[:6]:
        free = ('?' if row['free_liquidity_usd'] is None
                else f"${row['free_liquidity_usd']:,.0f}")
        print(f"  {row['pool']:<10s} {row['asset']:<8s} "
              f"{row['supply_apy'] * 100:5.2f}% + {row['emission_apr_gross'] * 100:5.2f}% "
              f"emis, free {free}")

    replay = _replay()
    if replay is None:
        return
    now = time.time()
    history = _window_history(now)
    if not history:
        return
    since = max(now - SCORE_WINDOW_S, float(history[0]['ts']))
    if now - since < MIN_SCORING_S:
        print(f'Rate history covers {(now - since) / 3600:.1f}h; '
              f'measurement 2 needs {MIN_SCORING_S / 3600:.0f}h')
        return

    def annualized(result):
        return result['return'] * (replay.SECONDS_PER_YEAR / result['covered_s']) * 100

    try:
        static = replay.best_static_ex_post(history, since, now)
        optimal = replay.optimal_rotation(history, since, now)
        null = _null_for(history, since, now)
    except Exception as e:
        print(f'measurement 2 unavailable ({e})')
        return
    if not (static and optimal and null):
        return
    edge_bp = (optimal['return'] - static['return']) * (
        replay.SECONDS_PER_YEAR / optimal['covered_s']) * 10000
    if abs(edge_bp) < 0.5:
        edge_bp = 0.0       # otherwise a rounding artefact prints as "-0bp"
    print(f"Over the last {optimal['covered_s'] / 3600:.0f}h, annualized: "
          f"null {annualized(null):.2f}%, best static {annualized(static):.2f}%, "
          f"optimal rotation {annualized(optimal):.2f}% "
          f"({optimal['rotations']} switches, {optimal['cost_bp']:.1f}bp of cost)")
    print(f'  YIELD.md measurement 2 -- what rotating was worth at all: '
          f'{edge_bp:+.0f}bp/yr. If this stays small, the domain is dead.')


def report_experiments():
    pass


def report_live(live_name):
    pass


def ensure_background_jobs():
    """Start the rate recorder if it is not running, then report what history exists.

    Idempotent and called once per cycle. The daemon is setsid'd out of monitor's process
    group so it outlives both a cycle and an emperor window -- which matters more here
    than for sdex, because the scoring window is three days and monitor is restarted
    roughly every twelve hours.

    It also guarantees a snapshot exists before returning. That is not tidiness: a
    strategy that starts with no snapshot allocates to nothing, logs nothing, and is
    `idle` by its first scoring -- and monitor.py's cull exempts a young strategy from
    the rank cull only if it is NOT idle (`age < YOUNG_GRACE_S and name not in
    idle_names`). So a missing snapshot at spawn time does not delay a strategy, it
    deletes it, three days of grace notwithstanding.
    """
    venues = _venues()
    if venues is None:
        print('yield_venues unavailable; no recorder started and no snapshot written')
        return

    if not background_jobs_alive():
        domain.TRADES_DIR.mkdir(parents=True, exist_ok=True)
        log = open(RECORDER_LOG, 'a')
        proc = subprocess.Popen(
            [_strategy_python(), '-u', str(RECORDER_SCRIPT),
             '--daemon', '--interval', str(RECORDER_INTERVAL)],
            stdout=log, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,       # survives a TERM to monitor's process group
        )
        RECORDER_PID_FILE.write_text(str(proc.pid))
        print(f'Started yield recorder (pid {proc.pid}, every {RECORDER_INTERVAL}s) '
              f'-> {RECORDER_LOG}')

    # The daemon's first sample is ~21s away; a spawn this cycle cannot wait for it.
    if venues.read_snapshot(max_age_s=SNAPSHOT_MAX_AGE_S) is None:
        print('No fresh venue snapshot; censusing synchronously so this cycle can spawn')
        venues.write_snapshot()

    try:
        import yield_recorder
    except Exception as e:
        print(f'yield_recorder unavailable ({e}); nothing is recording rates')
        return
    extent = yield_recorder.span()
    last = (yield_recorder.tail(1) or [{}])[0]
    age = round(time.time() - last['ts']) if last.get('ts') else None
    print(f"Rate history: {extent['rows']} rows over {extent['hours']}h; "
          f"last row {age}s ago, {len(last.get('rows') or [])} venues, "
          f"{len(last.get('live') or [])} strategies live")
    if age is not None and age > RECORDER_INTERVAL * 5:
        print(f'WARNING: rate history is {age}s stale. Nothing can be scored over a '
              f'window it does not cover. Check {RECORDER_LOG}')
    if extent['hours'] * 3600 < RANK_GRACE_S:
        print(f"Rate history covers {extent['hours']}h of the "
              f"{RANK_GRACE_S / 3600:.0f}h scoring window -- scores are provisional "
              f"until it fills")


def background_jobs_alive():
    """Is the recorder daemon running? Pid file plus two confirmations.

    `os.kill(pid, 0)` alone is not enough: the pid file survives a container restart and
    pids are recycled, so a stale file can name a live and entirely unrelated process, at
    which point monitor believes it has a recorder forever while nothing is being
    written. /proc/<pid>/cmdline settles it.
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
        return False        # cannot confirm -> assume not ours and respawn
    return 'yield_recorder' in cmdline


def _replay_facts():
    replay = _replay()
    if replay is None:
        return {}
    return {
        'rotation_cost_same_asset_bp': replay.SAME_ASSET_BP,
        'rotation_cost_cross_asset_bp': replay.CROSS_ASSET_BP,
        'emission_realization': replay.EMISSION_REALIZATION,
        # Renamed from null_min_liquidity_usd when the floor stopped being the null's
        # private business: it is enforced on every path now (yield_replay rule 4), and
        # a name saying otherwise is exactly the drift group 7 of the contract exists to
        # prevent. book_usd is published with it because under rule 4 the rate a venue
        # pays depends on how much arrives, so the floor alone no longer explains itself.
        'min_free_liquidity_usd': replay.MIN_FREE_LIQUIDITY_USD,
        'book_usd': replay.BOOK_USD,
    }


def prompt_facts():
    """Every number the revision prompt states about this domain, read live.

    Group 7 of the contract: the prompt told the model sdex's score haircut was 0.999 for
    weeks while score.py enforced 0.899, so nothing here is written as a literal in prose.
    """
    facts = {
        'starting_score': STARTING_SCORE,
        # Built from STARTING_SCORE rather than quoting it, for the reason this whole
        # group exists: the sdex prompt stated a haircut of 0.999 for weeks while
        # score.py enforced 0.899.
        'score_formula': (f'{STARTING_SCORE:.0f} plus the annualized EXCESS over the '
                          f'null in basis points, recomputed from your logged '
                          f'allocations priced against the recorded rate history. '
                          f'Matching the null scores exactly {STARTING_SCORE:.0f}; '
                          f'beating it by 1%/yr scores {STARTING_SCORE + 100:.0f}'),
        'score_is_self_reported': ('no. Nothing in state.json is read for scoring -- not '
                                   'apy_bp, not nav. Only the allocations you log, the '
                                   'rates the recorder saw, and whether your process was '
                                   'observed running'),
        'scoring_window_hours': SCORE_WINDOW_S / 3600,
        # No basis-point figure here on purpose: what downtime costs depends on the
        # null's rate, which is live data. The prompt derives the number from the window.
        'flat_when_stopped': ('a strategy the recorder does not see running earns '
                              'nothing for that time while the null keeps earning, so '
                              'downtime lowers the score rather than freezing it'),
        'cost_amplification': ('a one-off cost annualized over the scoring window is '
                               'multiplied by ~%.0f, so 1bp paid to rotate reads as '
                               '~%.0fbp of score. Rotating across ASSETS has to earn a '
                               'lot to pay for itself; rotating between pools lending '
                               'the SAME asset costs almost nothing'
                               % (SECONDS_PER_YEAR_HINT / SCORE_WINDOW_S,
                                  SECONDS_PER_YEAR_HINT / SCORE_WINDOW_S)),
        'null_baseline': ('allocate once to the highest-rate venue that has real free '
                          'liquidity, then never rotate'),
        'venues': 'Blend v2 lending pools that will accept a supply, Blend only',
        'aquarius_excluded_because': ('its pools cannot be entered single-sided, so a '
                                      'position there carries impermanent loss and two '
                                      'book crossings that nothing here prices yet'),
        'emissions_are_gross': ('BLND emission APRs are quoted before the cost of selling '
                                'BLND into its own book, which is why emission_weight is '
                                'a knob and not a constant.'),
        'live_trading': 'off, permanently, in this domain',
        'replay': ('your choose() is replayed over however much venue history the '
                   'recorder has collected, which grows as the container runs'),
        'rank_grace_s': RANK_GRACE_S,
        'knobs': sorted(DEFAULTS),
    }
    facts.update(_replay_facts())
    return facts

#!/usr/bin/env python3
"""Yield rotation across Blend's lending pools. A STUB, and deliberately a small one.

YIELD.md's "what to do, in order" puts `domain_yield.py` at step 4, after the two
measurements that decide whether the domain is worth building at all. This module exists
before those measurements so the container can be brought up and the loop exercised
against a real venue set -- not because the question is settled. Everything here is
paper. Nothing can be promoted. Read the four limits below before treating any number it
produces as evidence:

  1. **The score is self-reported.** `score()` reads an annualized yield out of the
     strategy's own state.json, exactly as domain_null does, and nothing audits it. The
     revision prompt says so out loud (see prompt_facts) because a model that notices can
     "win" by writing a large number into state.json, which measures nothing. Real
     scoring recomputes the allocation's return from the recorded rate history, and that
     history is YIELD.md step 2, which does not exist yet.
  2. **There is no replay.** `replay()` returns None -- the contract's "could not be
     measured", which every caller in the loop fails open on. A replay needs rate history
     and there is none: public Soroban RPC retains about seven days and contract state
     reads are current-value only. Until step 2 lands, revision gating here proves that
     code parses and runs, and nothing more.
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

# Score is STARTING_SCORE + the strategy's annualized net paper yield in basis points, so
# a strategy earning 5% shows 1500 and one paying more in rotation costs than it earns
# shows below 1000. Annualized rather than cumulative on purpose: a cumulative total makes
# score a function of age, every strategy's number climbs forever, and rank-culling ends
# up measuring birthdays (the trap domain_null's template docstring describes).

# YIELD.md's horizon section: rotation decisions arrive on a scale of days, not seconds,
# so a grace period sized for a domain whose feedback resolves in 30s would cull this one
# on the action-count tiebreak rather than on evidence. Three days is the document's own
# figure. Overridable because three days is also an unusable feedback loop for anyone
# testing the loop itself -- set YIELD_RANK_GRACE_S=3600 for that, and know that what you
# are then watching is noise.
RANK_GRACE_S = int(os.environ.get('YIELD_RANK_GRACE_S', 3 * 24 * 3600))

# No execution path exists (see limit 4), so a smoke run cannot place an order. PAPER_ONLY
# is set anyway: it costs nothing, tools/stellar_trader.py already honours it, and the day
# somebody adds an execution path to a strategy here they will do it in main.py without
# thinking about this constant.
SMOKE_ENV = {'PAPER_ONLY': '1'}

OBSERVE_FAILURE_NOTE = ('Could not read Blend pool rates (no fresh snapshot and the '
                        'chain read failed)')

# There is no replay, so these describe what a replay WOULD cover once step 2 exists.
REPLAY_DAYS = 7
REPLAY_WINDOW = 'the recorded rate history (does not exist yet -- YIELD.md step 2)'

# A snapshot older than this is not shown to the population. See yield_venues.read_snapshot:
# stale reads as absent, because an hour-old APY presented as current is the quiet failure.
SNAPSHOT_MAX_AGE_S = 2 * 3600


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
    """Refresh the venue snapshot and hand back the allocatable rows.

    The refresh is ~50s of RPC round trips, which is why it happens once per cycle here
    rather than once per strategy per tick: every main.py reads the file this writes.
    A failed refresh falls back to the last snapshot at any age, because a stale venue
    list is still a better basis for one cycle than none -- but the age travels with it
    so `observation_line` can say so.
    """
    venues = _venues()
    if venues is None:
        return None
    payload = venues.write_snapshot()
    if payload is None:
        payload = venues.read_snapshot(max_age_s=None)
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


def score(state_dict, obs):
    """STARTING_SCORE + the annualized net paper yield the strategy reported, in bp.

    Self-reported, and limit 1 in this module's docstring is about exactly that. The
    second element is the contract's list of things that could not be priced: here, any
    venue the strategy claims to hold that is not in this cycle's allocatable set, which
    is how a strategy parked in a pool that has since frozen becomes visible.
    """
    try:
        apy_bp = float(state_dict.get('apy_bp', 0.0))
    except (TypeError, ValueError):
        return STARTING_SCORE, ['apy_bp']
    unpriced = []
    if obs is not None:
        known = obs.by_key()
        for leg in state_dict.get('allocation') or []:
            key = (leg.get('pool_address'), leg.get('asset_address'))
            if key not in known:
                unpriced.append(f"{leg.get('pool')}/{leg.get('asset')}")
    return STARTING_SCORE + apy_bp, unpriced


def score_path(strategy_path, obs):
    state = _state_of(strategy_path)
    if state is None:
        return None
    return score(state, obs)[0]


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


def replay(strategy_dir):
    """None -- "could not be measured", which every caller fails open on.

    This is not a stub that someone forgot to fill in; there is nothing to replay against.
    A rotation backtest needs a rate history per venue, public RPC retains about a week of
    events and no state history at all, and reconstructing months of it out of an archive
    is YIELD.md step 2. Returning a fabricated pass here would be worse than returning
    nothing: the gates would report that a revision beat a null it was never tested on.
    """
    return None


def importability(source_or_path):
    """No replay engine imports the genome, so there is nothing to check."""
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
    try:
        nav = float(raw.get('nav'))
    except (TypeError, ValueError):
        return False, 'main.py wrote no numeric nav'
    legs = raw.get('allocation')
    if not isinstance(legs, list):
        return False, 'main.py wrote no allocation list'
    if not legs:
        return False, 'main.py allocated to nothing'
    where = ', '.join(f"{leg.get('pool')}/{leg.get('asset')}" for leg in legs[:3])
    return True, f'nav {nav:.6f} across {len(legs)} venue(s): {where}'


def cleanup_scratch(scratch_name):
    activity_log_path(scratch_name).unlink(missing_ok=True)


# --------------------------------------------------------------------- instruments

def report_activity(performances, limit=None):
    """Rotations against yield -- the turnover-vs-edge question, in this domain's units.

    A yield strategy that rotates often is paying a real cost for it, and this is where
    that shows up next to what it earned.
    """
    rows = []
    for name, _ in performances[:limit]:
        state = _state_of(Path(domain.STRATEGIES_DIR) / name)
        if not state:
            continue
        rows.append((name, int(state.get('rotations', 0) or 0),
                     float(state.get('apy_bp', 0.0) or 0.0),
                     float(state.get('cost_bp', 0.0) or 0.0)))
    if not rows:
        return
    print('Rotation vs yield:')
    for name, rotations, apy_bp, cost_bp in rows:
        print(f'  {name}: {rotations} rotation(s), {apy_bp:+.0f}bp annualized, '
              f'{cost_bp:.0f}bp paid in rotation cost')


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


def report_experiments():
    pass


def report_live(live_name):
    pass


def ensure_background_jobs():
    """The venue snapshot is the only background data here, and observe() refreshes it.

    This is the belt for the braces: monitor calls this on paths that do not observe, and
    a population reading a snapshot nobody refreshed would allocate on hours-old rates
    without anything saying so.
    """
    venues = _venues()
    if venues is None:
        return
    if venues.read_snapshot(max_age_s=SNAPSHOT_MAX_AGE_S) is None:
        venues.write_snapshot()


def background_jobs_alive():
    venues = _venues()
    if venues is None:
        return False
    return venues.read_snapshot(max_age_s=SNAPSHOT_MAX_AGE_S) is not None


def prompt_facts():
    """Every number the revision prompt states about this domain, read live.

    Group 7 of the contract: the prompt told the model sdex's score haircut was 0.999 for
    weeks while score.py enforced 0.899, so nothing here is written as a literal in prose.
    """
    return {
        'starting_score': STARTING_SCORE,
        'score_formula': ('STARTING_SCORE + the annualized net paper yield in basis '
                          'points that main.py writes to state.json as apy_bp'),
        'score_is_self_reported': ('yes -- nothing audits apy_bp. Writing a large number '
                                   'there raises the score and measures nothing, and the '
                                   'run is worthless if you do it'),
        'null_baseline': 'allocate once to the highest-rate venue and never rotate',
        'venues': 'Blend v2 lending pools that will accept a supply, Blend only',
        'aquarius_excluded_because': ('its pools cannot be entered single-sided, so a '
                                      'position there carries impermanent loss and two '
                                      'book crossings that nothing here prices yet'),
        'emissions_are_gross': ('BLND emission APRs are quoted before the cost of selling '
                                'BLND into its own book, which is why emission_weight is '
                                'a knob and not a constant'),
        'live_trading': 'off, permanently, in this domain',
        'replay': 'none -- no rate history exists, so revisions are not backtested',
        'rank_grace_s': RANK_GRACE_S,
        'knobs': sorted(DEFAULTS),
    }

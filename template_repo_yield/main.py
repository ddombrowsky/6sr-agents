#!/usr/bin/env python3
"""Yield-domain template strategy: allocate paper capital across Blend lending reserves.

Seed genome for `DOMAIN=yield`. Everything here is PAPER -- no order is placed, no
position is opened, nothing is signed. What is real is the venue data: the rates below
come from tools/yield_venues.py, which reads Blend's v2 pools off chain and keeps only
the ones that will actually accept a supply. See master_agent/domain_yield.py's docstring
for the four limits that apply to every number this file produces, and YIELD.md for why
the domain exists at all.

## Structure matters here -- read before editing

Same rule as template_repo/main.py and template_repo_null/main.py: the module top level
holds ONLY imports, assignments, function/class definitions, the docstring and the
`if __name__ == '__main__'` guard. The tick loop lives in `main()` under the guard, so
importing this module never starts it.

## The game

Each tick the strategy reads the allocatable venue list -- one row per (pool, reserve),
each carrying a base supply APY, a gross BLND emission APR, and how much free liquidity
the reserve has left. `choose()` picks which venues to hold. Between ticks the paper NAV
accrues at the rate of whatever is held, and a rotation is charged for.

**The two costs are not the same size, and that asymmetry is the whole strategy space.**
Moving between two pools that lend the SAME asset is a withdraw and a supply: no book is
crossed, and ROTATION_SAME_ASSET_BP is a transaction fee. Moving to a DIFFERENT asset
means swapping, twice over a round trip, and ROTATION_CROSS_ASSET_BP is sized from the
real XLM book. So chasing a 40bp rate improvement across assets loses money and chasing
the same 40bp across pools does not.

**Emissions are not worth their face value.** A BLND emission APR is quoted as if the
BLND were dollars, but it has to be sold into its own book first, and friction.py records
that book at 151-186bp against XLM's 12. Accrual here therefore credits emissions at
EMISSION_REALIZATION, a fixed haircut nobody's config can move. What a config CAN move is
`emission_weight`: how much of the gross emission APR to believe when RANKING venues. The
two are deliberately separate -- if believing in emissions also made you earn more, the
optimum would be "believe hardest", which measures nothing.

## What gets scored

domain_yield.score() reads `apy_bp` out of state.json: the net paper return over the last
WINDOW_S, annualized, in basis points. It is self-reported and nothing audits it. Writing
a big number there raises the score and measures nothing at all -- domain_yield says so
in its own docstring and in the revision prompt, and a run that does it is worthless.

Annualized over a rolling window rather than accumulated since birth, for the reason
template_repo_null/main.py gives: a lifetime total makes score a function of age, so the
oldest strategy is always on top and rank-culling measures birthdays.

`min_edge_bp`, `rebalance_hours`, `max_venues`, `emission_weight` and
`min_free_liquidity_usd` live in config.json, not here, so mechanical mutation can find
them without a code change. Read any knob you invent with `config.get('your_key',
<default>)`, never `config['your_key']`, so a fresh template spawn that never set it
still runs.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.append('/opt/tools')

CONFIG_PATH = Path('config.json')
STATE_PATH = Path('state.json')

# monitor.py reads this log through domain_yield.activity(). One JSON object per line
# with a `timestamp`, the same shape every other domain uses.
TRADES_DIR = Path('/opt/trades')

TICK_SECONDS = 60
SECONDS_PER_YEAR = 31536000.0

# The window `apy_bp` is measured over. Matched to domain_yield.RANK_GRACE_S: a score
# computed over a window longer than the loop waits before culling would rank strategies
# on a number that has not finished forming.
WINDOW_S = 3 * 24 * 3600
MIN_ELAPSED_S = 600          # below this the annualization is division by nearly zero
MAX_HISTORY = 2000

# A withdraw-and-supply between two pools lending the same asset crosses no order book,
# so this is transaction fees. Rotating to a different asset means swapping into it and,
# eventually, back out: two crossings of a book that friction.py measures at ~12bp for
# XLM. Both err high on purpose, the same way friction.py does -- a fitness landscape
# that under-charges for movement selects for churn, which is the failure at the top of
# FUTURE.md.
ROTATION_SAME_ASSET_BP = 1.0
ROTATION_CROSS_ASSET_BP = 25.0

# What a BLND emission is actually worth after it is sold. friction.py:16 records the
# non-XLM books at 151-186bp; the high end is the honest choice for a token you must sell
# rather than may sell. NOT config-reachable, deliberately -- see the docstring.
EMISSION_REALIZATION = 1.0 - 0.0175

# How stale a venue snapshot may be before this strategy refreshes it itself. Normally
# domain_yield.observe() refreshes it once per monitor cycle and every strategy just
# reads; this only fires in a fresh container where no cycle has run yet, which includes
# the smoke test of a brand-new revision.
SNAPSHOT_STALE_S = 2 * 3600


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception as e:
            print(f'could not read state.json ({e}); starting fresh')
    return {'nav': 1.0, 'apy_bp': 0.0, 'cost_bp': 0.0, 'rotations': 0,
            'allocation': [], 'last_rotation_ts': 0.0, 'history': []}


def save_state(state):
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f)


def log_event(agent_name, entry):
    """Append one allocation event. Never fatal: a strategy that cannot write its log is
    still holding a position, and dying here would look to monitor like a main.py that
    exited on its own."""
    try:
        TRADES_DIR.mkdir(parents=True, exist_ok=True)
        with (TRADES_DIR / f'{agent_name}.log').open('a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        print(f'could not write activity log ({e}); continuing')


def venue_rows():
    """The allocatable (pool, reserve) rows, or [] if none can be had.

    Refreshing is a ~50s chain read, so it happens only when the shared snapshot is
    missing or badly stale -- every ordinary tick is a file read.
    """
    try:
        import yield_venues
    except Exception as e:
        print(f'yield_venues unavailable ({e}); no venues this tick')
        return []
    payload = yield_venues.read_snapshot(max_age_s=SNAPSHOT_STALE_S)
    if payload is None:
        payload = yield_venues.write_snapshot() or yield_venues.read_snapshot(max_age_s=None)
    if payload is None:
        return []
    return yield_venues.allocatable_reserves(payload)


def venue_key(row):
    return (row['pool_address'], row['asset_address'])


def expected_apy(row, config):
    """What this strategy thinks a venue pays: base rate plus believed emissions.

    This is a RANKING number, not an accrual number. `emission_weight` is the belief and
    it moves only what gets chosen -- see accrue() for what is actually earned.
    """
    weight = float(config.get('emission_weight', 0.5))
    return row['supply_apy'] + weight * row['emission_apr_gross']


def realized_apy(row):
    """What a venue actually pays a paper holder: base rate plus emissions after the cost
    of selling the emission token. No config can reach this."""
    return row['supply_apy'] + EMISSION_REALIZATION * row['emission_apr_gross']


def eligible(rows, config):
    """Venues this strategy is willing to hold at all.

    `min_free_liquidity_usd` is the withdrawal check, applied before allocating rather
    than after halting (YIELD.md section 2). A reserve whose free liquidity is unknown is
    treated as ineligible when a floor is set: not knowing whether you could get out is
    not the same as knowing you could.
    """
    floor = float(config.get('min_free_liquidity_usd', 0.0))
    wanted = config.get('venues')
    out = []
    for row in rows:
        if row['utilization'] >= row['max_utilization']:
            continue
        if floor > 0:
            if row['free_liquidity_usd'] is None or row['free_liquidity_usd'] < floor:
                continue
        if isinstance(wanted, list) and wanted:
            if f"{row['pool']}/{row['asset']}" not in wanted:
                continue
        out.append(row)
    return out


def rotation_cost_bp(current_rows, target_rows):
    """Basis points charged for moving from one allocation to another.

    Charged on the fraction of NAV that actually moves, and priced by whether the move
    crosses assets. Holding costs nothing, which is the point: in this domain the correct
    action is frequently to do nothing.
    """
    current = {venue_key(r): r for r in current_rows}
    target = {venue_key(r): r for r in target_rows}
    if not current:
        return 0.0                      # the first allocation is a deposit, not a rotation
    leaving = [r for k, r in current.items() if k not in target]
    if not leaving:
        return 0.0
    held_assets = {r['asset_address'] for r in target.values()}
    moved_share = len(leaving) / max(1, len(current))
    cost = 0.0
    for row in leaving:
        rate = (ROTATION_SAME_ASSET_BP if row['asset_address'] in held_assets
                else ROTATION_CROSS_ASSET_BP)
        cost += rate / max(1, len(leaving))
    return cost * moved_share


def choose(rows, current, state, config, now):
    """Return the venue rows to hold. THE function a revision should be rewriting.

    `rows` is every allocatable venue this tick; `current` is what is held now, as rows
    from this same list (empty on the first tick). Pure and fast -- called once per tick
    and nothing else here depends on how long it takes.

    The default rule: hold the top `max_venues` by expected APY, but only move if the
    best candidate beats what is held by `min_edge_bp` AND the position has been held for
    `rebalance_hours`. Both hurdles exist because rotating is not free, and a rule that
    chases every basis point pays more in costs than the rates differ by.
    """
    candidates = eligible(rows, config)
    if not candidates:
        return current
    count = max(1, int(config.get('max_venues', 1)))
    ranked = sorted(candidates, key=lambda r: -expected_apy(r, config))
    target = ranked[:count]
    if not current:
        return target

    held_at = float(state.get('last_rotation_ts', 0.0))
    if now - held_at < float(config.get('rebalance_hours', 12.0)) * 3600:
        return current

    current_apy = sum(expected_apy(r, config) for r in current) / len(current)
    target_apy = sum(expected_apy(r, config) for r in target) / len(target)
    edge_bp = (target_apy - current_apy) * 10000
    if edge_bp < float(config.get('min_edge_bp', 50.0)):
        return current
    return target


def accrue(state, held_rows, elapsed_s):
    """Grow the paper NAV by what the held venues actually pay over `elapsed_s`.

    Simple interest over the tick, compounded by repetition -- the same thing Blend does,
    which accrues linearly between interactions and compounds because there are many.
    """
    if not held_rows or elapsed_s <= 0:
        return 0.0
    rate = sum(realized_apy(r) for r in held_rows) / len(held_rows)
    growth = rate * elapsed_s / SECONDS_PER_YEAR
    state['nav'] = float(state.get('nav', 1.0)) * (1.0 + growth)
    return growth


def charge(state, cost_bp):
    if cost_bp <= 0:
        return
    state['nav'] = float(state.get('nav', 1.0)) * (1.0 - cost_bp / 10000.0)
    state['cost_bp'] = round(float(state.get('cost_bp', 0.0)) + cost_bp, 4)


def update_window(state, now):
    """Recompute `apy_bp`: the net return over the last WINDOW_S, annualized.

    Prunes first, so a strategy that has gone flat sees its number decay rather than
    holding its last good one -- the window is emptying whether or not anything is being
    earned into it.
    """
    history = [pair for pair in state.get('history', []) if now - pair[0] < WINDOW_S]
    history.append([now, float(state.get('nav', 1.0))])
    del history[:-MAX_HISTORY]
    state['history'] = history

    oldest_ts, oldest_nav = history[0]
    elapsed = now - oldest_ts
    if elapsed < MIN_ELAPSED_S or oldest_nav <= 0:
        state['apy_bp'] = 0.0
        return
    total_return = float(state.get('nav', 1.0)) / oldest_nav - 1.0
    state['apy_bp'] = round(total_return * (SECONDS_PER_YEAR / elapsed) * 10000.0, 2)


def main():
    config = load_config()
    agent_name = config.get('name', 'unnamed')
    state = load_state()
    print(f"Agent {agent_name} starting with nav {state.get('nav', 1.0):.6f} "
          f"({state.get('rotations', 0)} rotations)")

    # Persist once before the first tick. monitor.py's smoke test reverts any revision
    # that has not written a readable state.json within SMOKE_TEST_SECONDS, and the first
    # venue read can be a chain refresh on a fresh container.
    save_state(state)

    last_tick = time.time()

    while True:
        now = time.time()
        rows = venue_rows()
        by_key = {venue_key(r): r for r in rows}

        # Defensive on purpose: `allocation` is written by this file, but this file is
        # what a revision rewrites, and a malformed leg must cost a tick rather than the
        # process. A strategy that dies here reads to monitor as a main.py that exited on
        # its own, which is a much harder failure to diagnose than a missing key.
        held = []
        for leg in state.get('allocation') or []:
            try:
                row = by_key.get(tuple(leg['key']))
            except (KeyError, TypeError):
                continue
            if row is not None:
                held.append(row)

        accrue(state, held, now - last_tick)
        last_tick = now

        if rows:
            try:
                target = choose(rows, held, state, config, now)
            except Exception as e:
                print(f'choose() raised {type(e).__name__}: {e}; holding')
                target = held

            if {venue_key(r) for r in target} != {venue_key(r) for r in held}:
                cost_bp = rotation_cost_bp(held, target)
                charge(state, cost_bp)
                first = not held
                state['rotations'] = int(state.get('rotations', 0)) + (0 if first else 1)
                state['last_rotation_ts'] = now
                state['allocation'] = [{
                    'key': list(venue_key(r)),
                    'pool': r['pool'], 'pool_address': r['pool_address'],
                    'asset': r['asset'], 'asset_address': r['asset_address'],
                    'weight': round(1.0 / len(target), 6),
                    'supply_apy': r['supply_apy'],
                    'emission_apr_gross': r['emission_apr_gross'],
                } for r in target]
                # The first allocation is logged as an action too. YIELD.md's horizon
                # section: `is_idle` demotes anything that has never logged one, and in a
                # yield domain the correct behaviour is frequently to hold.
                log_event(agent_name, {
                    'timestamp': now, 'name': agent_name,
                    'event': 'allocate' if first else 'rotate',
                    'venues': [f"{r['pool']}/{r['asset']}" for r in target],
                    'expected_apy': round(sum(expected_apy(r, config) for r in target)
                                          / len(target), 6),
                    'cost_bp': round(cost_bp, 4),
                    'nav': round(float(state['nav']), 8),
                })
                held = target

        update_window(state, now)
        save_state(state)
        time.sleep(TICK_SECONDS)


if __name__ == '__main__':
    main()

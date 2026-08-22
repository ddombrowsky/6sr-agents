#!/usr/bin/env python3
"""Recompute what a yield strategy actually earned, from evidence it does not control.

THE SPLIT THIS ENFORCES: the strategy supplies decisions, this module supplies rates,
costs and liveness. A strategy's only output is the sequence of allocations it logged;
everything that turns those into a number comes from tools/yield_recorder.py's history
and from the constants below. Writing a large number into state.json therefore does
nothing at all, which is the whole point -- domain_null scores a self-reported figure and
says plainly that a model which notices can win by lying, and this domain would have
inherited that hole.

WHAT IS RANKED IS EXCESS, NOT YIELD. Every strategy here collects roughly the base rate,
so absolute return is dominated by a component none of them chose: the venue set. Rank on
it and the population is sorted mostly by beta with a little skill on top, and the
differences between strategies are small relative to the number being compared -- the
*separation* problem YIELD.md's horizon section names, which is distinct from noise and
which waiting longer does not fix. Subtracting a contemporaneous null removes the common
component and leaves the part the strategy is responsible for.

THREE RULES THAT DECIDE RANKINGS, AND WHY THEY ARE HERE AND NOT IN A STRATEGY:

  1. **Not running means flat.** A supplied position keeps paying whether or not the
     process that chose it is alive, so a strategy stopped by the cull would go on
     accruing in any replay that did not know -- and monitor restarts top-N stopped
     strategies, so it could be resurrected on returns it earned while dead. Liveness
     comes from the recorder's pid check, observed from outside. A strategy cannot
     decline to be seen as stopped.

     Note what this does rather than freezing a score: flat earns nothing while the null
     goes on earning, so a stopped strategy's excess DECAYS every hour it stays down. It
     cannot climb back; it can only sink. Against a 6.6% null over a 3-day window, eight
     hours of downtime -- one monitor cycle in the deployed container -- costs about 73bp
     of annualized excess. Uptime is part of the strategy now, deliberately.

  2. **Involuntary transitions are free; chosen ones are charged.** Cost is computed
     between successive *intended* allocations, and an intended allocation only changes
     when the strategy logs an allocate or a rotate. Being stopped and later restarted by
     the loop does not change intent, so it costs nothing -- ranking strategies on when
     the scheduler happened to touch them would be measuring the loop, not them. The
     downtime penalty in rule 1 is already the price of being down.

  3. **Emissions are credited at a haircut no config can reach.** A BLND emission APR is
     quoted as if the BLND were dollars; it has to be sold into its own book first, and
     friction.py:16 records those books at 151-186bp against XLM's 12. A strategy's
     `emission_weight` decides only which venue it RANKS highest. If believing in
     emissions also made you earn more, the optimum would be "believe hardest", which
     measures nothing.

WHAT THIS IS NOT. It is not a backtest: it replays decisions that were actually made
against rates that were actually recorded. Simulating a *candidate* config over the same
history is the next use of this engine and is what will finally give domain_yield.replay()
something to return.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SECONDS_PER_YEAR = 31536000.0

# A withdraw-and-supply between two pools lending the same asset crosses no order book,
# so this is transaction fees. Changing asset means swapping into it and, eventually, back
# out: two crossings of a book friction.py measures at ~12bp for XLM and 151-186bp for
# everything else the population keeps trying to admit. Both err high, for the reason
# friction.py's design rule gives -- a fitness landscape that under-charges for movement
# selects for churn, which is the failure at the top of FUTURE.md.
SAME_ASSET_BP = 1.0
CROSS_ASSET_BP = 25.0

# What a BLND emission is worth after it is sold. See rule 3.
EMISSION_REALIZATION = 1.0 - 0.0175

# The null holds one venue for the whole window, so it must be one that could actually
# absorb capital. Without this the benchmark picks whatever dust pool tops the list --
# on 2026-08-21 that was Solv/USDC at 12.55% with $0 of free liquidity and $11 supplied,
# a rate no allocation could have earned.
NULL_MIN_LIQUIDITY_USD = 1000.0

CASH = ('cash', 'cash')
CASH_ASSET = 'cash'


def realized_apy(row):
    """What a venue actually pays a holder: base rate plus emissions after exit cost."""
    return (row.get('supply_apy') or 0.0) + EMISSION_REALIZATION * (
        row.get('emission_apr_gross') or 0.0)


# ------------------------------------------------------------------ strategy evidence

def load_events(log_path, until=None):
    """One strategy's log, parsed and cleaned. Oldest first.

    The log is written by the thing being scored, so it is treated as a claim rather than
    a record. Three claims are refused outright, all of which would otherwise let a
    strategy pick up a rate it never held:

      * a timestamp in the future,
      * a timestamp earlier than the entry before it (backdating into a better rate),
      * an entry that is not JSON.

    What it CANNOT lie about is what a venue paid, whether it was running, or what moving
    cost -- none of those come from this file.
    """
    events = []
    last_ts = 0.0
    try:
        with open(log_path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                ts = entry.get('timestamp')
                if not isinstance(ts, (int, float)):
                    continue
                if until is not None and ts > until:
                    continue
                if ts < last_ts:
                    continue
                last_ts = ts
                events.append(entry)
    except Exception:
        return []
    return events


def intent_changes(events):
    """[(ts, {venue_key: weight})] -- every point the strategy CHOSE a new allocation.

    `hold` is evidence the strategy is deciding rather than wedged, and `flat` is written
    on the way down by a strategy being stopped; neither is a decision, so neither
    changes intent. An allocate or rotate naming no venues IS a decision -- to sit in
    cash -- and is charged like any other.
    """
    changes = []
    for entry in events:
        if entry.get('event') not in ('allocate', 'rotate'):
            continue
        alloc = {}
        for leg in entry.get('allocation') or entry.get('legs') or []:
            key = (leg.get('pool_address'), leg.get('asset_address'))
            if None in key:
                continue
            try:
                alloc[key] = float(leg.get('weight') or 0.0)
            except (TypeError, ValueError):
                continue
        if not alloc:
            # Older/leaner log lines carry only display names in `venues`; the addresses
            # are recoverable from the history row nearest the event, so an event that
            # cannot be resolved to venue keys is kept as a change to cash rather than
            # silently ignored -- ignoring it would credit the strategy with a position
            # it may have left.
            alloc = {}
        changes.append((float(entry['timestamp']), alloc))
    return changes


# ---------------------------------------------------------------------- the cost model

def _normalized(alloc):
    """Weights plus an explicit cash leg, summing to 1. Over-allocation is scaled down."""
    total = sum(max(0.0, w) for w in alloc.values())
    if total <= 0:
        return {CASH: 1.0}
    out = {k: max(0.0, w) / max(1.0, total) for k, w in alloc.items()}
    spare = 1.0 - sum(out.values())
    if spare > 1e-9:
        out[CASH] = spare
    return out


def turnover_cost_bp(old, new):
    """Basis points to move from one intended allocation to another.

    Turnover is measured twice, once over venues and once over assets, because the two
    costs are not the same size and that asymmetry is the whole strategy space. The part
    of the move that changes ASSET has to cross a book, twice over a round trip. The part
    that only changes POOL is a withdraw and a supply against the same token. Aggregating
    venue weights by asset can only reduce turnover, so the difference between the two is
    exactly the pool-only fraction.
    """
    old_v, new_v = _normalized(old), _normalized(new)
    keys = set(old_v) | set(new_v)
    turnover_venue = sum(abs(new_v.get(k, 0.0) - old_v.get(k, 0.0)) for k in keys) / 2.0

    def by_asset(weights):
        out = {}
        for (_pool, asset), weight in weights.items():
            out[asset] = out.get(asset, 0.0) + weight
        return out

    old_a, new_a = by_asset(old_v), by_asset(new_v)
    assets = set(old_a) | set(new_a)
    turnover_asset = sum(abs(new_a.get(a, 0.0) - old_a.get(a, 0.0)) for a in assets) / 2.0
    turnover_asset = min(turnover_asset, turnover_venue)
    return (turnover_asset * CROSS_ASSET_BP
            + (turnover_venue - turnover_asset) * SAME_ASSET_BP)


# -------------------------------------------------------------------------- the replay

def _sample_index(history):
    """[(ts, {venue_key: row}, set(live))], oldest first."""
    out = []
    for row in history:
        venues = {}
        for entry in row.get('rows') or []:
            key = (entry.get('pool_address'), entry.get('asset_address'))
            if None in key:
                continue
            venues[key] = entry
        out.append((float(row['ts']), venues, set(row.get('live') or [])))
    out.sort(key=lambda item: item[0])
    return out


def run(history, changes, since, until, name=None):
    """Replay one allocation path. Returns a dict, never raises on ordinary bad data.

    `changes` is the intended-allocation timeline; `name` is whose liveness to honour
    (None means always live, which is what the null policies want -- a benchmark is not
    something that can crash).
    """
    samples = _sample_index(history)
    if not samples:
        return None
    covered_from = max(since, samples[0][0])
    covered_to = min(until, max(samples[-1][0], since))
    if covered_to <= covered_from:
        return None

    # Intent as of the start of the window: the last choice made at or before it.
    intent = {}
    for ts, alloc in changes:
        if ts <= covered_from:
            intent = alloc
    pending = [(ts, alloc) for ts, alloc in changes if covered_from < ts <= covered_to]

    checkpoints = sorted({covered_from, covered_to}
                         | {ts for ts, _, _ in samples if covered_from <= ts <= covered_to}
                         | {ts for ts, _ in pending})

    nav = 1.0
    cost_bp_total = 0.0
    flat_s = 0.0
    invested_s = 0.0
    rotations = 0
    sample_pos = 0

    for index, point in enumerate(checkpoints[:-1]):
        for ts, alloc in pending:
            if ts == point:
                # Free only out of an EMPTY intent, which is a strategy being funded for
                # the first time. An involuntary flat never clears intent (rule 2), so a
                # restart after a cull comes back through this branch with its previous
                # intent intact and is charged nothing -- while every real rotation is.
                if intent:
                    cost = turnover_cost_bp(intent, alloc)
                    nav *= (1.0 - cost / 10000.0)
                    cost_bp_total += cost
                    rotations += 1
                intent = alloc

        while (sample_pos + 1 < len(samples)
               and samples[sample_pos + 1][0] <= point):
            sample_pos += 1
        _ts, venues, live = samples[sample_pos]

        span = checkpoints[index + 1] - point
        if span <= 0:
            continue
        if name is not None and name not in live:
            flat_s += span
            continue
        rate = 0.0
        weight_used = 0.0
        for key, weight in _normalized(intent).items():
            if key == CASH:
                continue
            row = venues.get(key)
            if row is None:
                continue        # venue gone or frozen this sample: that leg earns nothing
            rate += weight * realized_apy(row)
            weight_used += weight
        if weight_used <= 0:
            flat_s += span
        else:
            invested_s += span
        nav *= (1.0 + rate * span / SECONDS_PER_YEAR)

    return {
        'return': nav - 1.0,
        'covered_s': covered_to - covered_from,
        'from': covered_from,
        'to': covered_to,
        'cost_bp': round(cost_bp_total, 4),
        'rotations': rotations,
        'flat_s': flat_s,
        'invested_s': invested_s,
    }


# ------------------------------------------------------------------------- the nulls

def _eligible(venues, floor=NULL_MIN_LIQUIDITY_USD):
    out = []
    for key, row in venues.items():
        if row.get('utilization', 0.0) >= row.get('max_utilization', 1.0):
            continue
        free = row.get('free_liquidity_usd')
        if free is None or free < floor:
            continue
        out.append((key, row))
    return out


def null_static_best(history, since, until):
    """Allocate once to the best venue that could absorb capital, then never rotate.

    YIELD.md's measurement 1 names this as the thing rotation has to beat: "allocate once
    and the null wins by construction" is the outcome if venue ordering never flips. It
    needs no foresight, so it is a benchmark rather than a bound.
    """
    samples = _sample_index(history)
    for ts, venues, _live in samples:
        if ts < since:
            continue
        eligible = _eligible(venues)
        if not eligible:
            continue
        key, _row = max(eligible, key=lambda item: realized_apy(item[1]))
        return run(history, [(ts, {key: 1.0})], since, until)
    return None


def null_equal_weight(history, since, until):
    """Hold every allocatable venue in equal weight, rebalanced free of charge.

    The market portfolio, and a deliberately unattainable one -- the free rebalancing is
    what makes it a diagnostic rather than a benchmark. Reported so a population that
    beats the static null can still be seen losing to simple breadth.
    """
    samples = _sample_index(history)
    changes = []
    for ts, venues, _live in samples:
        if ts < since or ts > until:
            continue
        eligible = _eligible(venues)
        if not eligible:
            continue
        weight = 1.0 / len(eligible)
        changes.append((ts, {key: weight for key, _row in eligible}))
    if not changes:
        return None
    result = run(history, changes, since, until)
    if result:
        result['cost_bp'] = 0.0
    return result


def best_static_ex_post(history, since, until):
    """The best single venue over the window, chosen with perfect hindsight.

    Measurement 2's DENOMINATOR, not its ceiling: YIELD.md asks what a perfect-foresight
    ROTATOR earns *against the best static allocation*, and this is the second half of
    that comparison. A rotator can and should beat it -- in the synthetic test a strategy
    that switched once at the right moment scored twice this -- which is exactly why it
    cannot be the bound.
    """
    samples = _sample_index(history)
    # Only venues that could actually have been ENTERED at the start of the window. The
    # same filter the null and the optimum apply, and leaving it off here is not a
    # rounding difference: on 2026-08-22 it let the benchmark "hold" Solv/USDC at 12.55%
    # with $0 of free liquidity and $11 supplied, which reported measurement 2 as -596bp
    # -- rotation looking catastrophically worse than a static allocation nothing could
    # have made. Three policies compared on three different universes is not a comparison.
    keys = set()
    for ts, venues, _live in samples:
        eligible = _eligible(venues)
        if eligible:
            keys = {key for key, _row in eligible}
            break
    best = None
    for key in keys:
        result = run(history, [(since, {key: 1.0})], since, until)
        if result and (best is None or result['return'] > best[1]['return']):
            best = (key, result)
    if best is None:
        return None
    key, result = best
    result['venue'] = key
    return result


def optimal_rotation(history, since, until):
    """The best rotation path that hindsight allows, solved exactly rather than guessed.

    Measurement 2's numerator, and with best_static_ex_post the whole of YIELD.md's kill
    criterion: **optimal_rotation minus best_static_ex_post is how much rotating is worth
    at all**, over this window, net of what moving costs. If that number is small the
    domain is dead before it starts, and it is computed here continuously off live data
    instead of waiting on the archive backfill step 2 calls for.

    Solved by dynamic programming over (sample, venue), in log space so the per-interval
    growth and the per-switch cost both add. A greedy rotator will not do: switching when
    the improvement covers the cost "over the rest of the window" is myopic -- it cannot
    see that it will want to switch back, and on the synthetic case it scored BELOW simply
    sitting in the venue that turned out best, which would report a ceiling underneath the
    strategies it is supposed to bound.

    Optimal over single-venue paths, which is the whole optimum here: returns are linear
    and certain within a sample, so blending across venues can only average the best rate
    down. Cash is a state like any other, and the first entry is free -- a strategy is not
    charged for being funded.
    """
    import math

    samples = _sample_index(history)
    samples = [item for item in samples if since <= item[0] <= until]
    if len(samples) < 2:
        return None

    states = {CASH}
    for _ts, venues, _live in samples:
        states.update(key for key, _row in _eligible(venues))
    states = sorted(states)
    cost = {(v, w): (0.0 if v == w else turnover_cost_bp({v: 1.0}, {w: 1.0}) / 10000.0)
            for v in states for w in states}

    # First entry is free, so every state starts level.
    value = {v: 0.0 for v in states}
    back = []
    for index in range(len(samples) - 1):
        ts, venues, _live = samples[index]
        span = samples[index + 1][0] - ts
        gain = {}
        for v in states:
            row = venues.get(v)
            rate = realized_apy(row) if row is not None else 0.0
            gain[v] = math.log1p(rate * span / SECONDS_PER_YEAR)
        nxt, choice = {}, {}
        for w in states:
            best_v, best_val = None, None
            for v in states:
                candidate = value[v] + gain[v] + math.log(max(1e-9, 1.0 - cost[(v, w)]))
                if best_val is None or candidate > best_val:
                    best_v, best_val = v, candidate
            nxt[w], choice[w] = best_val, best_v
        back.append(choice)
        value = nxt

    ts, venues, _live = samples[-1]
    final = {v: value[v] for v in states}
    end = max(final, key=lambda v: final[v])

    path = [end]
    for choice in reversed(back):
        path.append(choice[path[-1]])
    path.reverse()

    switches = sum(1 for a, b in zip(path, path[1:]) if a != b)
    return {
        'return': math.expm1(final[end]),
        'covered_s': samples[-1][0] - samples[0][0],
        'from': samples[0][0],
        'to': samples[-1][0],
        'cost_bp': round(sum(cost[(a, b)] for a, b in zip(path, path[1:])) * 10000.0, 4),
        'rotations': switches,
        'flat_s': 0.0,
        'invested_s': samples[-1][0] - samples[0][0],
        'venues': sorted({v for v in path if v != CASH}),
    }

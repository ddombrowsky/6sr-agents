#!/usr/bin/env python3
"""Offline replay for a MARKET MAKER: where a quote rests, whether it fills, what it cost.

This is the gate MAKER.md phase 1 exists to build, and the question it has to answer is
not "does a maker make money" but "can a fill even be modelled from the data we keep".
Everything downstream -- the offer lifecycle in stellar_trader, a whole new domain, a
container running real quotes -- is conditional on the answer, so this module is built and
run standalone BEFORE any of it.

WHAT MAKES A MAKER BACKTEST DIFFERENT. backtest.py replays a taker: at each candle it asks
`decide()` for a side and crosses the spread, and the fill is certain because crossing is
certain. A resting offer is the opposite -- the price is yours to choose and the fill is
not. It requires two data series this repo did not have a month ago:

  * the BOOK, at your price, at the moment you quoted   -- market_recorder ladder rows
  * the TAPE, between then and your next requote        -- dex_trades

and the join between them is the fill model in `_fill_usd`. Read that docstring before
trusting any number this module prints.

THE THREE NUMBERS THAT MATTER, and why the net is reported last:

  spread_captured_usd      what the quote earned against the mid it filled at
  adverse_selection_usd    what the mid then did to the inventory it left you holding
  net                      the difference, which is the only one anybody wants to read

A maker that quotes wide fills only when someone is in a hurry, and someone in a hurry is
usually right. Report gross spread capture alone and every width looks profitable. The
convention here is that `adverse_selection_usd` is signed the way it lands in P&L --
negative means the mid moved against the fill -- and that no caller may compute the net
without having been handed both halves.

DELIBERATELY PESSIMISTIC IN ONE DIRECTION, OPTIMISTIC IN ANOTHER, and both are documented
rather than netted: queue position assumes strict FIFO behind whatever depth was resting
when you joined and never credits a cancel ahead of you (pessimistic on fill QUANTITY),
while a one-minute snapshot cannot see the sub-second reprice that precedes an informed
fill (optimistic on fill QUALITY). These do not cancel. Live fills are the real
measurement and should be expected to be worse than this.
"""
import ast
import importlib.util
import json
import os
import time

import dex_trades
import market_recorder

START_USD = 1000.0          # same starting capital backtest.py uses, for comparability

# A quote's own price is never allowed inside this of the mid. Below it a "spread" is
# rounding noise on a $0.158 asset, and a backtest that fills there is measuring its own
# arithmetic. The live counterpart is stellar_trader.MIN_QUOTE_WIDTH_BP.
MIN_HALF_WIDTH_BP = 1.0

# Recorder rows are one a minute. A wider gap than this means the recorder was down, and
# fills must NOT be attributed across it: the tape kept printing while the book snapshot
# that would have told us where our quote sat did not.
MAX_GAP_S = 180

# Horizons, in minutes, at which every fill is marked against the mid. MAKER.md picks
# these; 5 is the headline because 1 is inside the noise of a 5-16 bp book and 15 is long
# enough that general drift starts to dominate the selection effect being measured.
MARK_HORIZONS_MIN = (1, 5, 15)
HEADLINE_HORIZON_MIN = 5

# Below this share of ticks backed by a real recorded ladder, the fill model is running on
# the shape profile in `_depth_profile` rather than on measured depth, and the result
# carries a WARNING. The ladder only started being recorded in phase 0, so a replay over
# older history is mostly profile.
MIN_LADDER_COVERAGE = 0.5

# Seconds between reading the book and a quote priced off it actually resting. Trades that
# printed inside that window cannot have filled it, and counting them would fill a quote
# against information it did not have when it was priced.
#
# This is NOT a nuisance parameter -- it is the single most load-bearing number in the
# module, and the sweep is acutely sensitive to it. Measured on the constant-width quoter
# over 8 days, net edge at a 5 bp half-width: +$5.08 at lag 0, +$2.02 at 5 s, +$0.50 at
# 10 s, -$0.15 at 15 s. Every width outside 5-6 bp is negative at every lag. So "does a
# maker have an edge here" is answered almost entirely by how fast a quote can be placed,
# and a backtest run at lag 0 would have reported a comfortable edge that does not exist.
#
# 5.0 is measured, not assumed, on this container against pubnet:
#     dex_price.get_orderbook       0.16 s
#     stellar CLI build + sign      0.25 s
#     submission round trip        ~0.5  s
#     wait for the next ledger      0-5.4 s (mean 2.7; measured close interval 5.4 s)
#   ------------------------------------------
#     ~3.6 s mean, ~6.3 s worst case
#
# Re-measure it if the submission path changes -- a slower signer moves the whole result.
FILL_LAG_S = 5.0


# ---------------------------------------------------------------------------
# book helpers
# ---------------------------------------------------------------------------

def _levels(row, side):
    """The recorded ladder for `side` as [(price, usd)], best first, or [] if absent."""
    raw = row.get('bids' if side == 'bid' else 'asks') or []
    out = []
    for lv in raw:
        try:
            price, usd = float(lv['p']), float(lv['usd'])
        except Exception:
            continue
        if price > 0 and usd >= 0:
            out.append((price, usd))
    out.sort(key=lambda pu: -pu[0] if side == 'bid' else pu[0])
    return out


def _depth_profile(rows):
    """Cumulative-depth-vs-distance curve, as a fraction of the row's aggregate depth.

    The fallback for rows recorded BEFORE the ladder existed. Those rows kept only the
    touch and one aggregate depth number per side, which cannot answer "how much is queued
    between the touch and my price" -- and that is the entire input to queue position.

    Rather than invent a shape, this measures one from the rows that DO carry a ladder and
    reuses it: for each such row, what fraction of that side's total depth sits within
    d basis points of the touch. It degrades honestly -- with no ladder rows at all it
    returns None and `_queue_ahead` falls back to "everything is ahead of you", which is
    the maximally pessimistic reading and cannot manufacture a fill.

    THE HORIZON MATTERS AS MUCH AS THE CURVE. The recorder keeps five levels, so a
    measured fraction stops growing past the fifth one -- not because the book ends there
    but because the record does. Read naively, the curve says "only 7% of the bid side is
    within 100 bp of the touch", which would let a quote 50 bp out look like it has almost
    nothing queued in front of it. So the fitted domain is capped at `horizon_bp`, the
    median distance to the worst level actually recorded, and `_queue_ahead` charges the
    FULL aggregate depth beyond it. Past the horizon this stops being an estimate and
    becomes a refusal to guess, in the pessimistic direction.

    Returned as {'bid': [(bp, frac)], 'ask': [...], 'horizon_bp': {'bid': x, 'ask': y}}.
    """
    grid = (0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0)
    acc = {'bid': [[] for _ in grid], 'ask': [[] for _ in grid]}
    reach = {'bid': [], 'ask': []}
    for row in rows:
        for side in ('bid', 'ask'):
            levels = _levels(row, side)
            touch = row.get('dex_bid' if side == 'bid' else 'dex_ask')
            total = row.get('bid_depth_usd' if side == 'bid' else 'ask_depth_usd')
            if not levels or not touch or not total:
                continue
            reach[side].append(abs(levels[-1][0] - touch) / touch * 10000.0)
            for i, bp in enumerate(grid):
                edge = (touch * (1 - bp / 10000.0) if side == 'bid'
                        else touch * (1 + bp / 10000.0))
                inside = sum(usd for price, usd in levels
                             if (price >= edge if side == 'bid' else price <= edge))
                acc[side][i].append(min(1.0, inside / total))
    profile = {'horizon_bp': {}}
    for side in ('bid', 'ask'):
        curve = []
        for i, bp in enumerate(grid):
            samples = acc[side][i]
            if samples:
                curve.append((bp, sum(samples) / len(samples)))
        if len(curve) < 2 or not reach[side]:
            return None
        ordered = sorted(reach[side])
        profile[side] = curve
        profile['horizon_bp'][side] = ordered[len(ordered) // 2]
    return profile


def _interp(curve, x):
    """Linear interpolation on [(x, y)] with flat extrapolation at both ends."""
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


def _queue_ahead(row, side, price, profile):
    """(usd_queued_ahead_of_a_quote_at_`price`, was_it_measured).

    "Ahead" is strict FIFO: everything resting at a better price, plus everything already
    resting AT our price, because we joined the back of that queue. It is not everything on
    the side -- a quote inside the touch has nothing ahead of it at all, and that case is
    exact even without a ladder, which is what makes a replay over pre-ladder history
    worth running.

    Four branches, in descending order of how much they can be trusted:

    1. Our price is strictly better than the touch -> 0, EXACT, and it needs no book at
       all. Nothing can be resting ahead of a quote that is itself the new best price.
       This branch is why a replay over history recorded before the ladder existed is
       still worth running: on this pair the touch is a couple of dollars of dust and the
       real depth sits a few bp behind it, so quoting inside the touch is the only maker
       behaviour that fills at all -- and it is exactly the case the old rows can price.
    2. A recorded cumulative-depth curve (`bid_cum`/`ask_cum`) -> measured, exact to the
       curve's grid, at any distance out to 200 bp. This is the normal path for rows
       recorded from phase 0 onward.
    3. Raw ladder levels but no curve -> measured over the five levels kept, with the
       unrecorded remainder of that side's aggregate depth charged against anything
       beyond them. Over-states rather than under-states.
    4. Neither -> the `_depth_profile` shape, scaled by this row's aggregate depth, and
       only out to that profile's measured horizon. Beyond the horizon the whole side is
       charged as ahead. An estimate, and flagged as one.
    """
    touch = row.get('dex_bid' if side == 'bid' else 'dex_ask')
    total = row.get('bid_depth_usd' if side == 'bid' else 'ask_depth_usd') or 0.0
    levels = _levels(row, side)

    better = (lambda p: p > price) if side == 'bid' else (lambda p: p < price)

    if touch and ((price > touch) if side == 'bid' else (price < touch)):
        return 0.0, True                    # strictly inside the touch: nothing ahead

    curve = row.get('bid_cum' if side == 'bid' else 'ask_cum')
    if curve and touch:
        try:
            grid = [(float(bp), float(usd)) for bp, usd in curve]
        except Exception:
            grid = []
        if len(grid) >= 2:
            distance_bp = abs(price - touch) / touch * 10000.0
            if distance_bp > grid[-1][0]:
                return max(total, grid[-1][1]), True
            return _interp(grid, distance_bp), True

    if levels:
        ahead = sum(usd for lp, usd in levels if better(lp) or lp == price)
        worst = levels[-1][0]
        beyond = (price < worst) if side == 'bid' else (price > worst)
        if beyond:
            # Deeper than the five levels we keep. Charge the unrecorded remainder.
            ahead += max(0.0, total - sum(usd for _, usd in levels))
        return ahead, True

    if not touch or not total or not profile:
        return total, False                 # know nothing: assume the whole side is ahead

    distance_bp = abs(price - touch) / touch * 10000.0
    if distance_bp > profile['horizon_bp'].get(side, 0.0):
        return total, False                 # past what the ladder ever recorded
    return total * _interp(profile[side], distance_bp), False


# ---------------------------------------------------------------------------
# the fill model
# ---------------------------------------------------------------------------

def _fill_usd(side, price, size_usd, ahead_usd, tape):
    """How much of a resting quote at `price` filled, in USD. The heart of the module.

    `tape` is every trade printed between this book snapshot and the next. A resting BID
    can only be filled by an aggressor SELLING, and vice versa -- which is why
    dex_trades._taker_side is the field the whole model rests on and why it has the
    docstring it has.

    Three quantities bound the fill, and it is the minimum of all three:

      size_usd                 you cannot fill more than you quoted.
      volume_total - ahead_usd  strict FIFO: every dollar that aggressed this side in the
                               window went to the queue in front of you first. NOTE this
                               uses ALL of the window's aggressing volume, not only the
                               volume that printed at or through your price -- the volume
                               that printed at BETTER prices is exactly what consumed the
                               queue ahead, and subtracting `ahead_usd` from a total that
                               excluded it would charge for the same depth twice. That
                               error makes every quote outside the touch look unfillable.
      volume_through           you did not fill at a price nothing traded at. A sweep that
                               stopped above your bid leaves you unfilled no matter how
                               large it was, and this is the term that says so.

    What it ignores, deliberately: cancels ahead of you (they would only help), and queue
    replenishment WITHIN the window (which would only hurt). The first is the larger
    effect and it is the pessimistic one, so the net bias is conservative -- but a
    one-minute window is doing real work here, and this is the assumption to attack first
    if the numbers ever look too good.
    """
    if size_usd <= 0 or price <= 0:
        return 0.0
    wants = 'sell' if side == 'bid' else 'buy'
    crosses = (lambda p: p <= price) if side == 'bid' else (lambda p: p >= price)
    volume_total = volume_through = 0.0
    for trade in tape:
        if trade.get('taker_side') != wants:
            continue
        usd = trade.get('usd') or 0.0
        volume_total += usd
        if crosses(trade.get('price') or 0.0):
            volume_through += usd
    return max(0.0, min(size_usd, volume_total - ahead_usd, volume_through))


# ---------------------------------------------------------------------------
# the strategy under test, and the null
# ---------------------------------------------------------------------------

_TOP_LEVEL_RULE = ('the module top level may only contain imports, assignments, defs, '
                   "the docstring and an `if __name__` guard")


def importability_report(main_py, entry='quote'):
    """(ok, reason) for whether this replay can import `entry` from a main.py.

    The same AST walk backtest.importability_report does, and for the same reason -- a
    top-level `while True` would hang the replay instead of failing it. It is a separate
    function only because that one hardcodes `node.name == 'decide'` (backtest.py:287),
    and a maker's entry point is `quote()`; MAKER.md §3.2 expected it to be reusable
    unchanged and it is not.
    """
    try:
        if hasattr(main_py, 'read_text'):
            source = main_py.read_text()
        elif isinstance(main_py, str) and not os.path.exists(main_py):
            source = main_py
        else:
            with open(main_py) as f:
                source = f.read()
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f'main.py has a syntax error: {e.msg} (line {e.lineno})'
    except Exception as e:
        return False, f'main.py could not be read: {e}'

    found = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == entry:
                found = True
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.Assign,
                             ast.AnnAssign, ast.Pass)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name) and test.left.id == '__name__'):
                continue
        try:
            text = ast.unparse(node).splitlines()[0][:57]
        except Exception:
            text = ''
        return False, (f'top-level {type(node).__name__} at line '
                       f'{getattr(node, "lineno", "?")}: {text} -- {_TOP_LEVEL_RULE}')
    if not found:
        return False, f'no top-level {entry}() function'
    return True, f'{entry}() is importable'


def _cfg(config, key, default):
    try:
        value = float(config.get(key, default))
    except Exception:
        return default
    return value if value == value else default          # reject NaN


def constant_width_quoter(config=None):
    """THE NULL: quote a fixed half-width off the touch, fixed size, no skew, no memory.

    This is the maker's buy-and-hold, and choosing it rather than "do nothing" is the
    whole reason `beats_null` means anything here. Do-nothing is a zero-return baseline
    that any positive-carry strategy clears without skill, and ranking a population
    against it would rank noise -- the same failure the sdex domain has with an hourly
    cull over a few price paths.

    It is deliberately NOT inventory-aware: managing inventory is a thing a real strategy
    does, and the null exists to price how much that is worth.
    """
    config = config or {}
    half_bp = max(MIN_HALF_WIDTH_BP, _cfg(config, 'half_width_bp', 8.0))
    size = _cfg(config, 'quote_usd', 50.0)

    def quote(book, state, cfg):
        mid = book.get('mid')
        if not mid:
            return None
        return {'bid': (mid * (1 - half_bp / 10000.0), size),
                'ask': (mid * (1 + half_bp / 10000.0), size)}
    return quote


def touch_quoter(config=None):
    """Quote AT the touch, improved by a hair, but never inside `half_width_bp` of the mid.

    The alternative anchor to the null's, and the one MAKER.md §3.1 expects a maker to
    use ("anchored to the DEX touch rather than to a remembered CEX price"). The
    difference is not cosmetic on this book: the spread moves between 5 and 16 bp, so a
    quote at a FIXED distance from the mid spends roughly half its life outside the touch,
    queued behind thousands of dollars, unable to fill at all. A touch-anchored quote
    steps just inside the current best bid/ask whenever the spread is wide enough to allow
    it, and falls back to the mid-anchored price when it is not.

    `half_width_bp` keeps its meaning as a FLOOR -- the closest to the mid this will ever
    quote -- rather than as the quote distance itself. That is what stops a tightening
    spread from dragging the quote in to where the capture no longer covers adverse
    selection.
    """
    config = config or {}
    half_bp = max(MIN_HALF_WIDTH_BP, _cfg(config, 'half_width_bp', 5.0))
    size = _cfg(config, 'quote_usd', 50.0)
    improve_bp = _cfg(config, 'improve_bp', 0.1)

    def quote(book, state, cfg):
        mid, bid, ask = book.get('mid'), book.get('bid'), book.get('ask')
        if not mid:
            return None
        floor_bid = mid * (1 - half_bp / 10000.0)
        floor_ask = mid * (1 + half_bp / 10000.0)
        want_bid = min(bid * (1 + improve_bp / 10000.0), floor_bid) if bid else floor_bid
        want_ask = max(ask * (1 - improve_bp / 10000.0), floor_ask) if ask else floor_ask
        return {'bid': (want_bid, size), 'ask': (want_ask, size)}
    return quote


def _quote_from_config(config):
    """The genome as a callable, for a strategy whose main.py cannot be imported.

    Mirrors backtest._decide_from_config: a config-only strategy is still a strategy, and
    a replay that refused to score one would make `importability` the gate instead of
    fitness. Adds the two knobs the null does not have -- inventory skew and a standdown
    band -- so a config-only genome can still express something the null cannot.
    """
    half_bp = max(MIN_HALF_WIDTH_BP, _cfg(config, 'half_width_bp', 8.0))
    size = _cfg(config, 'quote_usd', 50.0)
    skew_bp = _cfg(config, 'inventory_skew_bp', 0.0)
    band = _cfg(config, 'inventory_band_usd', 250.0)

    def quote(book, state, cfg):
        mid = book.get('mid')
        if not mid:
            return None
        inventory = state.get('inventory_usd', 0.0)
        # Long inventory pushes both quotes down: the bid gets less attractive and the
        # ask gets more, which is the standard way a maker leans out of a position
        # without crossing the spread to do it.
        lean = 0.0
        if band > 0 and skew_bp:
            lean = max(-1.0, min(1.0, inventory / band)) * skew_bp / 10000.0
        bid = (mid * (1 - half_bp / 10000.0 - lean), size)
        ask = (mid * (1 + half_bp / 10000.0 - lean), size)
        if band > 0 and inventory > band:
            bid = None              # too long to keep bidding
        if band > 0 and inventory < -band:
            ask = None
        return {'bid': bid, 'ask': ask}
    return quote


def _load_quote(strategy_dir, config):
    """(quote_fn, source). Falls back to the config genome, never raises."""
    main_py = os.path.join(strategy_dir or '', 'main.py')
    if not strategy_dir or not os.path.exists(main_py):
        return _quote_from_config(config), 'config-genome'
    if not importability_report(main_py)[0]:
        return _quote_from_config(config), 'config-genome'
    try:
        spec = importlib.util.spec_from_file_location(
            f'_mbt_{os.path.basename(strategy_dir.rstrip("/"))}', main_py)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.quote, 'main.py:quote'
    except Exception as e:
        print(f'[maker_backtest] could not import quote from {main_py}: {e}')
        return _quote_from_config(config), 'config-genome'


# ---------------------------------------------------------------------------
# the replay
# ---------------------------------------------------------------------------

def _load_rows(days, spec='XLM'):
    """Recorder rows with a usable two-sided book, oldest first."""
    rows = market_recorder.read_history(hours=float(days) * 24 + 1, spec=spec)
    return [r for r in rows
            if r.get('dex_bid') and r.get('dex_ask') and r.get('dex_mid')]


def _bucket_tape(trades, rows, lag_s=None):
    """Trades grouped by the recorder row whose interval they fall in.

    Returns a list parallel to `rows`: entry i is everything that printed in
    [rows[i].ts + FILL_LAG_S, rows[i+1].ts) -- see FILL_LAG_S for the lag. One pass over a sorted tape rather than a scan per row --
    a 8-day replay is ~11,000 rows against ~500,000 trades and the quadratic version does
    not finish.
    """
    buckets = [[] for _ in rows]
    if not rows:
        return buckets
    lag_s = FILL_LAG_S if lag_s is None else lag_s
    edges = [r['ts'] for r in rows]
    i = 0
    for trade in trades:
        ts = trade.get('ts') or 0
        while i + 1 < len(edges) and ts >= edges[i + 1]:
            i += 1
        if ts < edges[0] or ts < edges[i] + lag_s:
            continue
        buckets[i].append(trade)
    return buckets


def _mark_index(rows):
    """For each row, the index of the row closest to +h minutes, per MARK_HORIZONS_MIN."""
    marks = {h: [None] * len(rows) for h in MARK_HORIZONS_MIN}
    for h in MARK_HORIZONS_MIN:
        target = h * 60.0
        j = 0
        for i, row in enumerate(rows):
            want = row['ts'] + target
            if j < i:
                j = i
            while j + 1 < len(rows) and rows[j + 1]['ts'] <= want:
                j += 1
            # Only mark when the row we landed on is actually near the horizon; a gap in
            # the recorder must not turn into a 3-hour mark silently relabelled as 5 min.
            if abs(rows[j]['ts'] - want) <= MAX_GAP_S:
                marks[h][i] = j
    return marks


def replay(strategy_dir=None, days=7, spec='XLM', config=None, quote_fn=None,
           source=None, rows=None, tape=None, profile=None, lag_s=None, _null=True):
    """Replay one maker over `days` of recorded book and tape.

    Returns the contract MAKER.md §1.2 fixes, so domain_sdex_maker.replay() is a thin
    wrapper over it:

        {'return_pct', 'null_pct', 'beats_null', 'trades', 'fill_rate',
         'spread_captured_usd', 'adverse_selection_usd', 'inventory_max_usd',
         'quote_uptime_pct', 'decide_source', 'WARNING'}

    plus the diagnostics that say how much to believe it -- `ladder_exact_pct`,
    `adverse_selection_by_horizon`, `per_day`, and the two halves of the net.

    `rows`/`tape`/`profile` are an internal fast path so the null can be replayed against
    the identical data as the strategy instead of re-reading and re-bucketing 500k trades.
    """
    if config is None:
        config = {}
        path = os.path.join(strategy_dir or '', 'config.json')
        if strategy_dir and os.path.exists(path):
            try:
                with open(path) as f:
                    config = json.load(f)
            except Exception as e:
                print(f'[maker_backtest] could not read {path}: {e}')

    if rows is None:
        rows = _load_rows(days, spec)
    if len(rows) < 10:
        return {'error': 'not enough recorded book history to replay a maker'}
    if tape is None:
        tape = dex_trades.get_trades(spec=spec, start_ts=rows[0]['ts'],
                                     end_ts=rows[-1]['ts'] + MAX_GAP_S,
                                     sides_only=True)
    if not tape:
        return {'error': 'no trade tape cached; run dex_trades.backfill() first'}
    if profile is None:
        profile = _depth_profile(rows)

    buckets = _bucket_tape(tape, rows, lag_s)
    marks = _mark_index(rows)

    if quote_fn is None:
        quote_fn, source = _load_quote(strategy_dir, config)
    source = source or 'unknown'

    refresh_s = max(0.0, _cfg(config, 'refresh_s', 60.0))
    max_inventory = _cfg(config, 'max_inventory_usd', 400.0)

    balance_usd, balance_xlm = START_USD, 0.0
    live = {'bid': None, 'ask': None}       # (price, remaining_usd)
    quoted_at = None
    fills = []
    ticks = quoted_ticks = two_sided_ticks = exact_ticks = ladder_ticks = 0
    side_quotes = 0
    inventory_max = 0.0

    for i, row in enumerate(rows[:-1]):
        nxt = rows[i + 1]
        gap = nxt['ts'] - row['ts']
        if gap <= 0 or gap > MAX_GAP_S:
            live = {'bid': None, 'ask': None}
            quoted_at = None
            continue
        ticks += 1
        mid = row['dex_mid']
        book = {'bid': row['dex_bid'], 'ask': row['dex_ask'], 'mid': mid,
                'spread_bp': row.get('spread_bp'),
                'bids': row.get('bids') or [], 'asks': row.get('asks') or [],
                'bid_depth_usd': row.get('bid_depth_usd'),
                'ask_depth_usd': row.get('ask_depth_usd'),
                'ts': row['ts']}
        if row.get('bid_cum') and row.get('ask_cum'):
            ladder_ticks += 1

        inventory_usd = balance_xlm * mid
        inventory_max = max(inventory_max, abs(inventory_usd))

        # Requote only when the refresh interval has elapsed. Between refreshes the old
        # quote keeps resting at its old price, which is where a maker's adverse selection
        # actually comes from -- a stale quote is a free option written to the market, and
        # a model that repriced every tick would hide the whole cost of `refresh_s`.
        if quoted_at is None or (row['ts'] - quoted_at) >= refresh_s:
            state = {'balance_usd': balance_usd, 'balance_xlm': balance_xlm,
                     'inventory_usd': inventory_usd, 'mid': mid}
            try:
                decision = quote_fn(book, state, config)
            except Exception as e:
                print(f'[maker_backtest] quote() raised: {e}')
                decision = None
            live = {'bid': None, 'ask': None}
            if decision:
                for side in ('bid', 'ask'):
                    want = decision.get(side)
                    if not want:
                        continue
                    try:
                        price, size = float(want[0]), float(want[1])
                    except Exception:
                        continue
                    if price <= 0 or size <= 0:
                        continue
                    # A quote inside MIN_HALF_WIDTH_BP of the mid is not a quote, and a
                    # quote through the mid is a taker order wearing a maker's clothes.
                    edge_bp = abs(price - mid) / mid * 10000.0
                    if edge_bp < MIN_HALF_WIDTH_BP:
                        continue
                    if (side == 'bid' and price >= mid) or (side == 'ask' and price <= mid):
                        continue
                    live[side] = (price, size)
            quoted_at = row['ts']

        if live['bid'] or live['ask']:
            quoted_ticks += 1
        if live['bid'] and live['ask']:
            two_sided_ticks += 1

        window = buckets[i]
        for side in ('bid', 'ask'):
            if not live[side]:
                continue
            side_quotes += 1
            price, size = live[side]
            ahead, exact = _queue_ahead(row, side, price, profile)
            if exact:
                exact_ticks += 1
            filled = _fill_usd(side, price, size, ahead, window)
            if filled <= 0:
                continue
            # Clamp to what the account could actually settle. A bid cannot spend cash it
            # does not have and an ask cannot deliver XLM it is not holding; without this
            # the replay would happily run a strategy into an inventory no real account
            # could carry and report the P&L of a position that could not exist.
            if side == 'bid':
                filled = min(filled, balance_usd)
                if max_inventory > 0:
                    filled = min(filled, max(0.0, max_inventory - inventory_usd))
                if filled <= 0:
                    continue
                balance_usd -= filled
                balance_xlm += filled / price
            else:
                filled = min(filled, balance_xlm * price)
                if max_inventory > 0:
                    filled = min(filled, max(0.0, max_inventory + inventory_usd))
                if filled <= 0:
                    continue
                balance_usd += filled
                balance_xlm -= filled / price
            inventory_usd = balance_xlm * mid
            inventory_max = max(inventory_max, abs(inventory_usd))
            live[side] = (price, size - filled)
            fills.append({'i': i, 'side': side, 'price': price, 'usd': filled,
                          'mid': mid, 'ts': row['ts']})

    # ----- decomposition -------------------------------------------------
    # Spread capture is measured against the mid AT THE FILL, which is the only
    # definition that does not smuggle in the price move being measured separately below.
    spread_captured = 0.0
    for fill in fills:
        edge = ((fill['mid'] - fill['price']) if fill['side'] == 'bid'
                else (fill['price'] - fill['mid']))
        spread_captured += fill['usd'] * edge / fill['mid']

    adverse = {}
    for h in MARK_HORIZONS_MIN:
        total = 0.0
        marked = 0
        for fill in fills:
            j = marks[h][fill['i']]
            if j is None:
                continue
            later = rows[j]['dex_mid']
            move = (later - fill['mid']) / fill['mid']
            # A bid fill leaves you long: a mid that falls afterwards is the cost. A sell
            # leaves you short the inventory you just gave up, so the sign flips.
            total += fill['usd'] * (move if fill['side'] == 'bid' else -move)
            marked += 1
        adverse[h] = {'usd': round(total, 4), 'marked': marked}

    final_mid = rows[-1]['dex_mid']
    net_worth = balance_usd + balance_xlm * final_mid
    headline_adverse = adverse[HEADLINE_HORIZON_MIN]['usd']

    covered_days = max(1e-9, (rows[-1]['ts'] - rows[0]['ts']) / 86400.0)
    ladder_pct = ladder_ticks / ticks if ticks else 0.0
    exact_pct = exact_ticks / side_quotes if side_quotes else 0.0

    warnings = []
    if ladder_pct < MIN_LADDER_COVERAGE:
        warnings.append(
            f'only {ladder_pct * 100:.0f}% of ticks had a recorded book ladder; queue '
            f'position on the rest is the measured depth PROFILE, not measured depth')
    if not fills:
        warnings.append('zero fills: nothing here measures spread capture at all')
    if covered_days < 1:
        warnings.append(f'only {covered_days:.2f} days of book history covered')

    result = {
        'strategy': os.path.basename((strategy_dir or 'null').rstrip('/')),
        'decide_source': source,
        'days': days,
        'covered_days': round(covered_days, 2),
        'ticks': ticks,
        'trades': len(fills),                # FILLS, not requotes -- see the loop's gate
        'buys': sum(1 for f in fills if f['side'] == 'bid'),
        'sells': sum(1 for f in fills if f['side'] == 'ask'),
        'fills_per_day': round(len(fills) / covered_days, 1),
        'fill_rate': round(len(fills) / side_quotes, 5) if side_quotes else 0.0,
        'quote_uptime_pct': round(100.0 * two_sided_ticks / ticks, 2) if ticks else 0.0,
        'one_sided_uptime_pct': round(100.0 * quoted_ticks / ticks, 2) if ticks else 0.0,
        'volume_usd': round(sum(f['usd'] for f in fills), 2),
        'spread_captured_usd': round(spread_captured, 4),
        'adverse_selection_usd': headline_adverse,
        'adverse_selection_by_horizon': {f'{h}m': adverse[h] for h in MARK_HORIZONS_MIN},
        'net_edge_usd': round(spread_captured + headline_adverse, 4),
        'final_usd': round(balance_usd, 4),
        'final_xlm': round(balance_xlm, 4),
        'final_net_worth': round(net_worth, 2),
        'return_pct': round((net_worth / START_USD - 1) * 100, 4),
        'inventory_max_usd': round(inventory_max, 2),
        'ladder_coverage_pct': round(100.0 * ladder_pct, 1),
        'queue_exact_pct': round(100.0 * exact_pct, 1),
        'WARNING': '; '.join(warnings) or None,
    }

    if _null:
        null = replay(strategy_dir=None, days=days, spec=spec,
                      config={'half_width_bp': _cfg(config, 'null_half_width_bp', 8.0),
                              'quote_usd': _cfg(config, 'quote_usd', 50.0),
                              'refresh_s': 60.0,
                              'max_inventory_usd': max_inventory},
                      quote_fn=constant_width_quoter(
                          {'half_width_bp': _cfg(config, 'null_half_width_bp', 8.0),
                           'quote_usd': _cfg(config, 'quote_usd', 50.0)}),
                      source='null:constant-width',
                      rows=rows, tape=tape, profile=profile, _null=False)
        result['null_pct'] = null.get('return_pct')
        result['null_trades'] = null.get('trades')
        result['null_net_edge_usd'] = null.get('net_edge_usd')
        result['null_inventory_max_usd'] = null.get('inventory_max_usd')
        # beats_null is decided on NET EDGE, not on return_pct, and the difference is not
        # cosmetic. A maker's return_pct is dominated by whatever inventory it happened to
        # be holding when the window closed, marked at that minute's mid -- which is beta,
        # not skill. The measured case: the null has no inventory management by
        # construction, so on this pair (where ~88% of aggressing flow is selling) it runs
        # its bid into the inventory cap and sits there, and its return_pct becomes a
        # levered bet on XLM that swamps a spread capture three orders of magnitude
        # smaller. Ranking on that would select for whoever guessed the drift.
        #
        # net_edge_usd = spread captured - adverse selection is the part a maker controls.
        # It cannot be gamed by simply not quoting, because the loop separately gates on
        # `trades` (fills), and a strategy with no fills has no edge to report.
        # return_pct is still returned, and beats_null_return alongside it, because the
        # account balance is the thing that is ultimately real.
        result['beats_null'] = (result['net_edge_usd'] > null.get('net_edge_usd')
                                if null.get('net_edge_usd') is not None else None)
        result['beats_null_return'] = (result['return_pct'] > result['null_pct']
                                       if result['null_pct'] is not None else None)
    return result


def sweep(widths=(2, 3, 5, 8, 12, 20, 30, 40), days=7, quote_usd=50.0, spec='XLM',
          refresh_s=60.0):
    """Replay the constant-width quoter at each half-width. The kill-criterion instrument.

    MAKER.md's phase-1 gate is a statement about this table, not about any single run:
    proceed only if the naive quoter fills at all, and if its edge NET of adverse
    selection is positive at SOME width in the band -- and if that is not one day or one
    $490 trade carrying the whole result, which is what `per_day` is for.
    """
    rows = _load_rows(days, spec)
    if len(rows) < 10:
        return {'error': 'not enough recorded book history'}
    tape = dex_trades.get_trades(spec=spec, start_ts=rows[0]['ts'],
                                 end_ts=rows[-1]['ts'] + MAX_GAP_S, sides_only=True)
    if not tape:
        return {'error': 'no trade tape cached'}
    profile = _depth_profile(rows)
    out = {'days': days, 'quote_usd': quote_usd, 'rows': len(rows), 'tape': len(tape),
           'widths': {}}
    for bp in widths:
        config = {'half_width_bp': float(bp), 'quote_usd': quote_usd,
                  'refresh_s': refresh_s}
        res = replay(days=days, spec=spec, config=config,
                     quote_fn=constant_width_quoter(config),
                     source=f'null:constant-width:{bp}bp',
                     rows=rows, tape=tape, profile=profile, _null=False)
        out['widths'][f'{bp}bp'] = {
            k: res.get(k) for k in
            ('trades', 'fills_per_day', 'fill_rate', 'volume_usd',
             'spread_captured_usd', 'adverse_selection_usd', 'net_edge_usd',
             'return_pct', 'inventory_max_usd', 'quote_uptime_pct',
             'ladder_coverage_pct', 'queue_exact_pct')}
    return out


def per_day(half_width_bp=8.0, days=7, quote_usd=50.0, spec='XLM'):
    """The same replay cut into 24h slices, so a result can be checked for stability.

    A net edge that is positive overall and positive on one day out of seven is not an
    edge, it is one trade. MAKER.md's third kill criterion is this table's sign column.
    """
    rows = _load_rows(days, spec)
    if len(rows) < 10:
        return {'error': 'not enough recorded book history'}
    tape = dex_trades.get_trades(spec=spec, start_ts=rows[0]['ts'],
                                 end_ts=rows[-1]['ts'] + MAX_GAP_S, sides_only=True)
    profile = _depth_profile(rows)
    config = {'half_width_bp': half_width_bp, 'quote_usd': quote_usd, 'refresh_s': 60.0}
    out = {'half_width_bp': half_width_bp, 'days': {}}
    start = rows[0]['ts']
    for d in range(int(days) + 1):
        lo, hi = start + d * 86400, start + (d + 1) * 86400
        slice_rows = [r for r in rows if lo <= r['ts'] < hi]
        if len(slice_rows) < 60:
            continue
        slice_tape = [t for t in tape if lo <= t['ts'] < hi]
        res = replay(days=1, spec=spec, config=config,
                     quote_fn=constant_width_quoter(config),
                     source='null:constant-width', rows=slice_rows, tape=slice_tape,
                     profile=profile, _null=False)
        label = time.strftime('%Y-%m-%d', time.gmtime(lo))
        out['days'][label] = {k: res.get(k) for k in
                              ('trades', 'spread_captured_usd', 'adverse_selection_usd',
                               'net_edge_usd', 'return_pct', 'volume_usd')}
    signs = [v['net_edge_usd'] for v in out['days'].values()
             if v.get('net_edge_usd') is not None]
    if signs:
        out['positive_days'] = sum(1 for s in signs if s > 0)
        out['total_days'] = len(signs)
        out['largest_day_share'] = (round(max(abs(s) for s in signs) / sum(abs(s) for s in signs), 3)
                                    if sum(abs(s) for s in signs) else None)
    return out


if __name__ == '__main__':
    import sys
    days = 7.0
    if '--days' in sys.argv:
        days = float(sys.argv[sys.argv.index('--days') + 1])
    size = 50.0
    if '--size' in sys.argv:
        size = float(sys.argv[sys.argv.index('--size') + 1])
    if '--sweep' in sys.argv:
        print(json.dumps(sweep(days=days, quote_usd=size), indent=2))
    elif '--per-day' in sys.argv:
        bp = 8.0
        if '--width' in sys.argv:
            bp = float(sys.argv[sys.argv.index('--width') + 1])
        print(json.dumps(per_day(half_width_bp=bp, days=days, quote_usd=size), indent=2))
    else:
        target = None
        for arg in sys.argv[1:]:
            if not arg.startswith('--') and os.path.isdir(arg):
                target = arg
        print(json.dumps(replay(target, days=days), indent=2))

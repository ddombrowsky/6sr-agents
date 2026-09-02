#!/usr/bin/env python3
"""The maker's execution seam: turn a desired quote into resting offers and fills.

`trade_logger.execute_trade` is this module's taker counterpart, and the division of
labour is the same one: a strategy's `main.py` decides WHERE to rest and HOW BIG, and
nothing else. Placement, replacement, cancellation, fill detection, balance mutation,
inventory accounting and logging all live here.

WHY THAT LINE IS DRAWN HERE AND NOT NEGOTIABLE. A revision LLM rewrites `main.py`, and the
one thing it must never rewrite is the part that touches money. For the taker that was
`execute_trade`; for a maker the surface is larger and more dangerous, because an offer
outlives the process that placed it. A `main.py` that manages its own offers can leave
them resting after it dies, after it is culled, or after a leader change -- a position
opened on behalf of a strategy that no longer exists, which nothing is watching.
`domain_sdex_maker.can_execute_live` therefore requires a call to `sync_quotes` by name,
and `check_smoke_state` fails a candidate that finishes with offers still open.

PAPER AND LIVE USE THE SAME FILL MODEL, deliberately. `maker_backtest._fill_usd` and
`_queue_ahead` are imported here rather than reimplemented, so a paper strategy's fills
and its backtest's fills are produced by the same code against the same recorded book and
tape. That is the only way `replay()`'s numbers and the leaderboard's numbers can be
compared to each other -- and the taker side of this system has spent weeks at a time with
a backtest measuring something subtly different from the live loop.

THE TAPE IS READ, NEVER FETCHED. Fills are computed from `dex_trades`' on-disk cache, which
one background daemon keeps current (`dex_trades.py --sync-daemon`, supervised by
`domain_sdex_maker.ensure_background_jobs`). Ten strategies each paging Horizon /trades on
a 30s loop is a rate-limit incident, which is the same reason `market_recorder` is a
single writer with many readers.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import portfolio as _portfolio
from trade_logger import record_trade

BASE_DIR = Path('/opt/trades')
LIVE_FLAG = Path('live.flag')

# How far back to look for fills when a strategy has no recorded last-sync time (a fresh
# clone, or one whose state.json was reset). Bounded so a strategy that has been stopped
# for a day does not wake up and book a day of fills against a quote it was not resting.
_MAX_CATCHUP_S = 300.0


def _paper_only():
    return bool(os.environ.get('PAPER_ONLY'))


def _is_live(explicit=None):
    """Whether this strategy may place real offers.

    Same resolution as trade_logger.execute_trade: `live.flag` in the strategy's own
    working directory, which strat_manager sets as cwd. PAPER_ONLY overrides it in the
    closing direction and can never open it -- the smoke test runs a candidate for real,
    and a candidate that talked itself past this would rest real money.
    """
    if _paper_only():
        return False
    if explicit is not None:
        return bool(explicit)
    return LIVE_FLAG.exists()


def _tools():
    """(dex_trades, maker_backtest, market_recorder) or (None, None, None)."""
    try:
        import dex_trades
        import maker_backtest
        import market_recorder
        return dex_trades, maker_backtest, market_recorder
    except Exception as e:
        print(f'[quote_executor] fill model unavailable ({e})')
        return None, None, None


def _stellar():
    try:
        import stellar_trader
        return stellar_trader
    except Exception as e:
        print(f'[quote_executor] stellar_trader unavailable ({e})')
        return None


def _caps():
    """The §2.4 caps, read live off stellar_trader, with inert fallbacks.

    Falls back to values that cannot authorise anything a strategy could not already do
    on its own -- a missing trading module must not read as "no limit".
    """
    st = _stellar()
    if st is None:
        return {'max_open_offers': 0, 'per_side': 0.0, 'total': 0.0,
                'max_age_s': 900.0, 'min_width_bp': 2.0, 'max_skew_usd': 0.0}
    return {'max_open_offers': st.MAX_OPEN_OFFERS,
            'per_side': st.MAX_RESTING_USD_PER_SIDE,
            'total': st.MAX_RESTING_USD_TOTAL,
            'max_age_s': st.MAX_OFFER_AGE_S,
            'min_width_bp': st.MIN_QUOTE_WIDTH_BP,
            'max_skew_usd': st.MAX_INVENTORY_SKEW_USD}


def _normalize(state):
    state = _portfolio.normalize_state(state or {})
    if not isinstance(state.get('open_offers'), list):
        state['open_offers'] = []
    return state


def inventory_usd(state, mid):
    """Signed USD value of the XLM leg.

    Resting offers are deliberately NOT added on top, and the reason is worth stating
    because the opposite is intuitive. This executor does not RESERVE on placement:
    `positions` and `balance_usd` move when something fills and at no other time. So the
    XLM behind a resting ask is still sitting in `positions['XLM']`, and adding the
    offer's amount again would count it twice -- a maker would see its inventory double
    the moment it quoted and skew hard against a position it had not taken.

    This mirrors Stellar itself, where a resting offer does not reduce `balance` either;
    it raises `selling_liabilities` alongside it. See score's counterpart note in
    domain_sdex_maker.
    """
    state = _normalize(state)
    return _portfolio.get_amount(state, 'XLM') * (mid or 0.0)


def _clamp_decision(decision, state, book, caps, config, live):
    """The desired quote, reduced to what the caps actually permit.

    Returns {'bid': (price, usd) | None, 'ask': ...}. A strategy's `quote()` output is a
    REQUEST, exactly as `execute_trade`'s `requested_usd` is: nothing a revision can write
    widens what follows.

    THE SIZE AND INVENTORY CAPS APPLY TO LIVE MONEY ONLY, and that is not a loophole. The
    caps in stellar_trader bound REAL exposure -- MAX_RESTING_USD_PER_SIDE is $4, against
    a paper book that starts at $1,000. Applying them to paper would quote $4 into a book
    whose touch is routinely $2 deep, produce a handful of fills a day, and leave the
    leaderboard ranking sampling noise -- which is the exact failure this whole domain
    exists to escape. The paper size is bounded instead by the genome, through
    domain_sdex_maker.MAX_QUOTE_USD in config_is_sane. Live, every one of these is
    re-checked inside stellar_trader.place_offer against the real account, so this
    function is a convenience there and not the enforcement.

    What DOES apply to both is the width floor and the no-crossing rule: a quote through
    the mid is a taker order in disguise in paper and in live alike, and letting the paper
    book fill on one would make the replay and the leaderboard measure different games.
    """
    out = {'bid': None, 'ask': None}
    if not decision:
        return out
    mid = book.get('mid')
    if not mid or mid <= 0:
        return out
    inv = inventory_usd(state, mid)
    for side in ('bid', 'ask'):
        want = decision.get(side)
        if not want:
            continue
        try:
            price, usd = float(want[0]), float(want[1])
        except Exception:
            continue
        if price <= 0 or usd <= 0 or price != price or usd != usd:
            continue
        width_bp = abs(price - mid) / mid * 10000.0
        if width_bp < caps['min_width_bp']:
            continue
        # A "quote" through the mid is a taker order in disguise: it would cross on
        # submission and spend past every cap that governs crossing.
        if (side == 'bid' and price >= mid) or (side == 'ask' and price <= mid):
            continue
        if live:
            # One-sided standdown at the inventory limit. A CAP, not the strategy's own
            # skew logic -- a genome may lean earlier and can never lean later.
            if caps['max_skew_usd'] > 0:
                if side == 'bid' and inv >= caps['max_skew_usd']:
                    continue
                if side == 'ask' and inv <= -caps['max_skew_usd']:
                    continue
            if caps['per_side'] > 0:
                usd = min(usd, caps['per_side'])
        if usd <= 0:
            continue
        out[side] = (price, usd)
    # Settle-ability is checked at FILL time, not here, and that is what keeps the paper
    # book identical to maker_backtest: the replay quotes the full size and clamps what
    # fills to what the account could deliver. Clamping the quote instead would stop a
    # flat maker from ever showing an ask, because it starts with no XLM -- so it would
    # never sell, never round-trip, and never look like a maker at all.
    if live and caps['total'] > 0:
        total = sum(q[1] for q in out.values() if q)
        if total > caps['total'] > 0:
            scale = caps['total'] / total
            for side in ('bid', 'ask'):
                if out[side]:
                    out[side] = (out[side][0], out[side][1] * scale)
    return out


def _book_row():
    """The most recent recorded book row, or None.

    market_recorder.tail(1) rather than a live fetch: it is the single-writer transport
    and the only function in that module a 30s loop may call.
    """
    _, _, recorder = _tools()
    if recorder is None:
        return None
    rows = recorder.tail(1)
    return rows[-1] if rows else None


def _apply_fill(state, agent_name, side, price, usd, mid, live_result=None):
    """Book one fill: mutate balances, keep `positions` authoritative, write the log line.

    The log line goes through trade_logger.record_trade in the v2 shape, so
    live_report.py, monitor.trade_stats and domain_sdex_maker.activity all keep working
    unchanged -- and so `activity()` counts FILLS. A maker that logged its requotes would
    clear MIN_LIVE_TRADES in ten minutes with no evidence of anything.
    """
    if usd <= 0 or price <= 0:
        return state
    amount_xlm = usd / price
    if side == 'bid':
        state['balance_usd'] = float(state.get('balance_usd') or 0.0) - usd
        _portfolio.add_amount(state, 'XLM', amount_xlm)
        action = 'maker_buy'
        norm_side = 'buy'
    else:
        state['balance_usd'] = float(state.get('balance_usd') or 0.0) + usd
        _portfolio.add_amount(state, 'XLM', -amount_xlm)
        action = 'maker_sell'
        norm_side = 'sell'
    _portfolio.sync_legacy(state)
    try:
        record_trade(agent_name, action, mid or price, usd,
                     amount_xlm if norm_side == 'buy' else -amount_xlm,
                     state.get('balance_usd', 0.0), state.get('balance_xlm', 0.0),
                     asset='XLM', live=live_result, fill_price=price,
                     friction_bp=(round(abs(price - mid) / mid * 10000.0, 3)
                                  if mid else None))
    except Exception as e:
        print(f'[quote_executor] could not log fill: {e}')
    return state


# ---------------------------------------------------------------------------
# paper
# ---------------------------------------------------------------------------

def _paper_fills(state, agent_name, row, since_ts, now_ts):
    """Fill the offers currently resting in `state`, using the backtest's own model.

    Imported from maker_backtest rather than restated: queue position, the aggressor
    filter and the "volume through our price" bound are all subtle enough that a second
    implementation would drift, and the whole value of the paper book is that it is
    comparable to the replay.

    THE OBSERVE->REST LAG IS CHARGED HERE, and it has to be charged somewhere. The
    backtest applies it in `maker_backtest._bucket_tape`, which drops every trade printed
    within FILL_LAG_S of the book row a quote was priced from; `_fill_usd` itself takes a
    tape and a price and knows nothing about when the quote appeared. So sharing the
    matcher is not enough to share the model -- a paper book that hands `_fill_usd` the
    raw window is running at lag 0, which is the one thing MAKER_PHASE1.md says a maker
    backtest must not do. Over the phase-1 sample a 5 bp half-width is +$5.08 at lag 0 and
    +$2.02 at lag 5: the difference is 60% of the entire measured edge, and it is the
    difference between a comfortable result and a sliver.

    Anchored on each offer's own `placed_ts`, not on `since_ts`. `since_ts` is the TAPE
    watermark, which trails wall clock by however long ago the sync daemon last paged
    Horizon, so a window starting there begins BEFORE the quote existed -- the paper book
    was crediting fills to trades that printed while it was still deciding what to quote,
    on top of not charging the lag at all. Per-offer rather than per-window because the
    two sides are replaced independently and a partially filled offer keeps its original
    placement time.
    """
    dex_trades, mbt, _ = _tools()
    if dex_trades is None or not row:
        return state, 0
    try:
        tape = dex_trades.get_trades(start_ts=since_ts, end_ts=now_ts, sides_only=True)
    except Exception as e:
        print(f'[quote_executor] tape unavailable ({e})')
        return state, 0
    if not tape:
        return state, 0
    mid = row.get('dex_mid')
    filled = 0
    # Read off maker_backtest so the two cannot drift apart silently; 0.0 only if that
    # module ever drops the constant, in which case the old lag-0 behaviour is at least
    # explicit rather than accidental.
    lag_s = float(getattr(mbt, 'FILL_LAG_S', 0.0) or 0.0)
    for offer in list(state.get('open_offers') or []):
        side, price = offer.get('side'), float(offer.get('price') or 0.0)
        remaining = float(offer.get('usd') or 0.0)
        if side not in ('bid', 'ask') or price <= 0 or remaining <= 0:
            continue
        # An offer written before this field existed falls back to the window start, which
        # still charges the lag -- never to "no floor", which would restore the bug.
        placed = float(offer.get('placed_ts') or 0.0)
        floor = (placed if placed > 0 else since_ts) + lag_s
        eligible = [t for t in tape if (t.get('ts') or 0.0) >= floor]
        if not eligible:
            continue
        ahead, _exact = mbt._queue_ahead(row, side, price, None)
        got = mbt._fill_usd(side, price, remaining, ahead, eligible)
        if got <= 0:
            continue
        if side == 'bid':
            got = min(got, float(state.get('balance_usd') or 0.0))
        else:
            got = min(got, _portfolio.get_amount(state, 'XLM') * price)
        if got <= 0:
            continue
        state = _apply_fill(state, agent_name, side, price, got, mid)
        offer['usd'] = remaining - got
        offer['amount_xlm'] = offer['usd'] / price
        filled += 1
    state['open_offers'] = [o for o in state['open_offers']
                            if float(o.get('usd') or 0.0) > 1e-9]
    return state, filled


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------

def _live_sync(state, agent_name, targets, row):
    """Reconcile on-chain offers into `state`, then place/replace/cancel to match targets.

    Reconcile FIRST. Placing before reconciling would book a replacement against balances
    that still contain a fill we have not noticed, and the replace itself destroys the
    evidence -- `--offer-id` is an atomic cancel/replace, so the shrunken amount that
    would have told us about the fill is gone.
    """
    st = _stellar()
    if st is None:
        return state, 0, {'ok': False, 'reason': 'stellar_trader unavailable'}

    expected = {str(o['offer_id']): o for o in (state.get('open_offers') or [])
                if o.get('offer_id')}
    report = st.reconcile_offers(expected)
    mid = (row or {}).get('dex_mid')
    filled = 0
    for fill in report.get('fills') or []:
        record = expected.get(str(fill['offer_id'])) or {}
        price = float(record.get('price') or fill.get('price') or 0.0)
        side = record.get('side') or fill.get('side')
        filled_usd = float(fill['filled_usd'])
        # amount_usd is not decoration: live_report sums exactly this key to get the live
        # notional, and without it every maker promotion reads back as "$0.00 live, ratio
        # 0.000, live-sized +0.00%" no matter what actually filled -- the live-vs-paper
        # comparison the promotion is supposed to be audited by, silently reading zero.
        state = _apply_fill(state, agent_name, side,
                            price, filled_usd, mid,
                            live_result={'submitted': True, 'detected': 'reconcile',
                                         'amount_usd': filled_usd})
        # Book it against the daily spend budget. Without this the cap is unwired on the
        # maker path entirely -- _record_spend is only reached from submit_trade.
        try:
            st.record_fill_spend(filled_usd, side)
        except Exception as e:
            print(f'[quote_executor] could not record spend: {e}')
        filled += 1

    # On-chain truth replaces our belief, every tick. Anything resting that we did not
    # place shows up here as an offer with no placed_ts, and gets aged out below like any
    # other -- it is not ours to keep and not safe to leave.
    resting = {o['id']: o for o in report.get('resting') or []}
    rebuilt = []
    for offer_id, live_offer in resting.items():
        known = expected.get(offer_id) or {}
        rebuilt.append({'offer_id': offer_id, 'side': live_offer['side'],
                        'price': live_offer['price'], 'usd': live_offer['usd'],
                        'amount_xlm': live_offer['amount_xlm'],
                        'placed_ts': known.get('placed_ts') or time.time()})
    state['open_offers'] = rebuilt

    now = time.time()
    caps = _caps()
    by_side = {}
    for offer in list(state['open_offers']):
        age = now - float(offer.get('placed_ts') or now)
        if age > caps['max_age_s']:
            # MAX_OFFER_AGE_S is a safety cap, not a strategy knob: a stale quote is a
            # free option written to the market, and it is cancelled here whatever the
            # strategy would prefer.
            st.cancel_offer(offer['offer_id'], offer['side'])
            state['open_offers'].remove(offer)
            continue
        by_side.setdefault(offer['side'], offer)

    for side in ('bid', 'ask'):
        want = targets.get(side)
        have = by_side.get(side)
        if want is None:
            if have:
                st.cancel_offer(have['offer_id'], side)
                state['open_offers'] = [o for o in state['open_offers']
                                        if o['offer_id'] != have['offer_id']]
            continue
        price, usd = want
        if have and abs(have['price'] - price) / price < 1e-6 \
                and abs(have['usd'] - usd) < 0.01:
            continue                            # already resting where we want it
        result = st.place_offer(side, usd, price,
                                offer_id=int(have['offer_id']) if have else 0)
        if not result.get('submitted'):
            print(f'[{agent_name}] {side} not rested: {result.get("reason")}')
            continue
        state['open_offers'] = [o for o in state['open_offers']
                                if not (have and o['offer_id'] == have['offer_id'])]
        state['open_offers'].append({
            'offer_id': result.get('offer_id'), 'side': side,
            'price': result.get('price', price), 'usd': result.get('usd', usd),
            'amount_xlm': result.get('usd', usd) / price,
            'placed_ts': time.time()})
    return state, filled, report


# ---------------------------------------------------------------------------
# the seam
# ---------------------------------------------------------------------------

def sync_quotes(agent_name, decision, state, config=None, *, book=None, is_live=None):
    """Make the market look like `decision`, book whatever filled, and return `state`.

    THE ONLY function a maker's main.py may call to act on a quote. Call it once per tick
    with whatever `quote()` returned (or None to stand down on both sides).

    agent_name: the log filename, as in trade_logger.record_trade.
    decision:   {'bid': (price, usd) | None, 'ask': (price, usd) | None} or None.
                A REQUEST. Every cap in stellar_trader is applied on top of it.
    state:      the strategy's balances dict, normalised and mutated in place.
    book:       the current book row; read from market_recorder if omitted.
    is_live:    normally None -- resolved from live.flag in the cwd. PAPER_ONLY always
                wins.

    Returns `state`. Never raises: this runs inside a strategy's tick loop, and an
    exception here stops a process that strat_manager only starts once.
    """
    try:
        state = _normalize(state)
        row = book or _book_row()
        if not row or not row.get('dex_mid'):
            return state
        now = time.time()
        # Fills may only be attributed over the interval the TAPE actually covers, which
        # trails wall-clock by however long ago the sync daemon last paged Horizon. Using
        # `now` as the end of the window looks harmless and is not: the window
        # [since, now] would be advanced past trades that had not been fetched yet, and
        # the next tick would start after them. Every fill in the lag would be skipped,
        # permanently, and the symptom is a paper maker that quotes correctly and never
        # fills -- which is indistinguishable from a bad width.
        covered = now
        dex_trades, _mbt, _rec = _tools()
        if dex_trades is not None:
            try:
                covered = float(dex_trades.span().get('last_ts') or now)
            except Exception:
                covered = now
        since = float(state.get('last_quote_sync_ts') or 0.0)
        if since <= 0 or covered - since > _MAX_CATCHUP_S:
            since = covered - _MAX_CATCHUP_S

        live = _is_live(is_live)
        caps = _caps()
        targets = _clamp_decision(decision, state, {'mid': row.get('dex_mid')},
                                  caps, config or {}, live)

        if live:
            state, filled, _report = _live_sync(state, agent_name, targets, row)
        else:
            # Paper: fill what was resting BEFORE replacing it. Reversed, a quote would
            # be filled by the tape of a minute during which it was resting somewhere
            # else -- the same ordering error _live_sync avoids by reconciling first.
            state, filled = _paper_fills(state, agent_name, row, since, covered)
            state['open_offers'] = [
                {'offer_id': None, 'side': side, 'price': price, 'usd': usd,
                 'amount_xlm': usd / price, 'placed_ts': now}
                for side, (price, usd) in
                ((s, targets[s]) for s in ('bid', 'ask') if targets.get(s))]

        # The tape watermark, not the clock: see `covered` above.
        state['last_quote_sync_ts'] = covered if not live else now
        state['quoted_sides'] = sum(1 for s in ('bid', 'ask') if targets.get(s))
        state['fills_total'] = int(state.get('fills_total') or 0) + filled
        _portfolio.sync_legacy(state)
        return state
    except Exception as e:
        print(f'[quote_executor] sync_quotes failed: {e}')
        return state


def stand_down(agent_name, state, *, is_live=None):
    """Pull every quote. Called on shutdown and by the domain's retire path.

    Live, this goes through stellar_trader.cancel_all_offers rather than cancelling the
    ids we think we have: an offer we lost track of is exactly the one that needs
    cancelling, and on-chain truth is the only list that contains it.
    """
    state = _normalize(state)
    if _is_live(is_live):
        st = _stellar()
        if st is not None:
            result = st.cancel_all_offers()
            if not result.get('ok'):
                print(f'[{agent_name}] offers still resting: {result}')
                state['open_offers'] = [o for o in st.open_offers()]
                return state
    state['open_offers'] = []
    return state

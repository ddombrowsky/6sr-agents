#!/usr/bin/env python3
"""Standalone self-test for the maker data path: dex_trades, the ladder, the fill model.

    python3 /opt/tools/selftest_maker.py

Same shape as selftest_portfolio.py -- no test runner exists in this repo, so this is a
plain script that exits 0 with a pass count or 1 with the offending values.

WHY THIS FILE IS WORTH ITS LENGTH. Every number the maker plan turns on is produced by
three functions, and all three fail SILENTLY when they are wrong:

  dex_trades._taker_side   labelled 98% of the tape as taker-buys under a plausible and
                           entirely wrong reading of base_is_seller. Nothing crashed. The
                           only symptom was a market that appeared to trade in one
                           direction, and the sign of every adverse-selection number
                           downstream depended on it.
  _queue_ahead             returned a queue from the 5-level ladder that stopped growing
                           past ~3 bp, because the RECORD stopped there and not the book.
                           Read naively that makes a quote 40 bp out look unqueued.
  _fill_usd                subtracting the queue ahead from volume that had already
                           passed that queue double-charges it, and makes every quote
                           outside the touch unfillable.

The fixtures below are hand-computed sweeps with the arithmetic written out in the
comments, so a future change that "simplifies" one of these can be checked against a
worked example rather than against whatever it currently returns.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dex_trades
import maker_backtest as mb
import market_recorder

_passed = 0
_failures = []


def check(label, condition, detail=''):
    global _passed
    if condition:
        _passed += 1
    else:
        _failures.append(f'{label}{": " + detail if detail else ""}')


def close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


# --------------------------------------------------------------- aggressor direction

# Real Horizon /trades records, verbatim, for XLM(base)/USDC(counter). The direction of
# each was established independently by fetching the aggressing operation and reading its
# own source/destination assets -- see _taker_side's docstring for the experiment.
_MAKER_ON_BASE = {          # op: path_payment_strict_send USDC -> XLM, so the taker BOUGHT
    'trade_type': 'orderbook', 'ledger_close_time': '2026-08-17T02:58:57Z',
    'base_amount': '983.6064668', 'counter_amount': '155.7049037',
    'base_offer_id': '1853567467', 'counter_offer_id': '4886517608444555266',
    'base_is_seller': True, 'price': {'n': '172917781', 'd': '1092596232'},
    'paging_token': '274831886370078721-0',
}
_MAKER_ON_COUNTER = {       # op: path_payment_strict_send XLM -> USDC, so the taker SOLD
    'trade_type': 'orderbook', 'ledger_close_time': '2026-08-17T02:56:10Z',
    'base_amount': '15.2587575', 'counter_amount': '2.4149121',
    'base_offer_id': '4886517608444555266', 'counter_offer_id': '1853567467',
    'base_is_seller': False, 'price': {'n': '172917781', 'd': '1092596232'},
    'paging_token': '274831860599689217-0',
}
_POOL = dict(_MAKER_ON_BASE, trade_type='liquidity_pool',
             paging_token='274831860599689217-9')

check('taker side: maker sold base -> ask consumed -> buy',
      dex_trades._taker_side(_MAKER_ON_BASE) == 'buy',
      str(dex_trades._taker_side(_MAKER_ON_BASE)))
check('taker side: maker bought base -> bid consumed -> sell',
      dex_trades._taker_side(_MAKER_ON_COUNTER) == 'sell',
      str(dex_trades._taker_side(_MAKER_ON_COUNTER)))
check('taker side: missing field is unknown, not a guess',
      dex_trades._taker_side({'trade_type': 'orderbook'}) is None)

# The offer ids must NOT be what decides the direction. Flip only base_is_seller and the
# answer must flip -- if it does not, the offer-id theory has crept back in.
_flipped = dict(_MAKER_ON_BASE, base_is_seller=False)
check('taker side follows base_is_seller alone',
      dex_trades._taker_side(_flipped) == 'sell', str(dex_trades._taker_side(_flipped)))

check('utc parse is not local time',
      dex_trades._parse_time('2026-08-17T02:58:57Z') == 1786935537,
      str(dex_trades._parse_time('2026-08-17T02:58:57Z')))

row = dex_trades._row(_MAKER_ON_BASE)
check('row keeps the price rational, not a float', row['n'] == 172917781 and row['d'] == 1092596232)
check('row usd is the counter (USDC) leg', close(row['c'], 155.7049037))
check('orderbook row carries no pool marker', 'x' not in row)
check('pool row is kept but marked', dex_trades._row(_POOL).get('x') == 'p')

check('paging tokens compare numerically, not lexically',
      dex_trades._token_key('9-1') < dex_trades._token_key('10-1'))

check('horizon params use the base_asset prefix Horizon actually reads',
      'base_asset_type' in dex_trades._pair_params()
      and dex_trades._pair_params()['base_asset_type'] == 'native',
      str(dex_trades._pair_params()))

# ------------------------------------------------------------------- ladder recording

_BOOK_LEVELS = [{'price': 0.1583, 'usd': 2.0}, {'price': 0.1582, 'usd': 100.0},
                {'price': 0.1581, 'usd': 400.0}, {'price': 0.1570, 'usd': 9000.0}]
_cum = market_recorder._cum_depth(_BOOK_LEVELS, 0.1583, 'bid')
_at = dict((bp, usd) for bp, usd in _cum)
# 0.1582 is (0.1583-0.1582)/0.1583 = 6.32 bp below the touch; 0.1581 is 12.63 bp below.
check('cum depth at the touch counts the touch itself', close(_at[0.5], 2.0))
check('cum depth at 7 bp includes the 6.32 bp level', close(_at[7], 102.0), str(_at[7]))
check('cum depth at 15 bp includes the 12.63 bp level', close(_at[15], 502.0), str(_at[15]))
check('cum depth far out includes everything', close(_at[200], 9502.0), str(_at[200]))
check('cum depth of an empty book is absent, not zero',
      market_recorder._cum_depth([], 0.1583, 'bid') is None)

_ask_cum = dict(market_recorder._cum_depth(
    [{'price': 0.1584, 'usd': 5.0}, {'price': 0.1585, 'usd': 50.0}], 0.1584, 'ask'))
check('ask cum walks upward from the ask touch', close(_ask_cum[7], 55.0), str(_ask_cum[7]))

# ------------------------------------------------------------------- queue position

_ROW = {'ts': 0, 'dex_bid': 0.1583, 'dex_ask': 0.1584, 'dex_mid': 0.15835,
        'bid_depth_usd': 20000.0, 'ask_depth_usd': 20000.0,
        'bids': [{'p': p, 'usd': u} for p, u in
                 ((0.1583, 2.0), (0.1582, 100.0), (0.1581, 400.0))],
        'bid_cum': _cum, 'ask_cum': market_recorder._cum_depth(
            [{'price': 0.1584, 'usd': 5.0}, {'price': 0.1585, 'usd': 50.0}], 0.1584, 'ask')}

ahead, exact = mb._queue_ahead(_ROW, 'bid', 0.15835, None)
check('a bid inside the touch has nothing ahead of it', close(ahead, 0.0) and exact)

ahead, exact = mb._queue_ahead(_ROW, 'bid', 0.1583, None)
check('a bid AT the touch queues behind the size already there',
      close(ahead, 2.0) and exact, f'{ahead} exact={exact}')

ahead, exact = mb._queue_ahead(_ROW, 'bid', 0.1581, None)
check('a bid two levels down queues behind both', ahead >= 102.0 and exact, str(ahead))

ahead, exact = mb._queue_ahead(_ROW, 'bid', 0.1583 * (1 - 0.05), None)  # 500 bp out
check('past the recorded curve, the whole side is charged as ahead',
      close(ahead, 20000.0), str(ahead))

# A row from before the ladder existed: only the touch and one aggregate number.
_OLD = {'ts': 0, 'dex_bid': 0.1583, 'dex_ask': 0.1584, 'dex_mid': 0.15835,
        'bid_depth_usd': 20000.0, 'ask_depth_usd': 20000.0}
ahead, exact = mb._queue_ahead(_OLD, 'bid', 0.15835, None)
check('inside the touch is exact even with no ladder at all',
      close(ahead, 0.0) and exact, f'{ahead} exact={exact}')
ahead, exact = mb._queue_ahead(_OLD, 'bid', 0.1580, None)
check('outside the touch with no ladder and no profile is maximally pessimistic',
      close(ahead, 20000.0) and not exact, f'{ahead} exact={exact}')

# ------------------------------------------------------------------- the fill model

def _tape(*pairs):
    """[(price, usd, taker_side)] as the Trade dicts _fill_usd consumes."""
    return [{'price': p, 'usd': u, 'taker_side': s} for p, u, s in pairs]


# The worked example from _fill_usd's docstring. Book bids: 0.1583 $100, 0.1582 $200,
# 0.1581 $300. We rest $50 at 0.1581, so ahead = 100 + 200 + 300 = 600.
#
# A taker sells $500: it prints $100@.1583, $200@.1582, $200@.1581 and stops. The $200
# that reached our level went to the $300 queued in front of us, so we get nothing.
sweep_500 = _tape((0.1583, 100, 'sell'), (0.1582, 200, 'sell'), (0.1581, 200, 'sell'))
check('a sweep that stops inside the queue ahead fills nothing',
      close(mb._fill_usd('bid', 0.1581, 50, 600, sweep_500), 0.0),
      str(mb._fill_usd('bid', 0.1581, 50, 600, sweep_500)))

# The same taker sells $700 instead: $100 + $200 + $400. 700 - 600 = 100 of headroom, so
# our whole $50 fills. This is the case a "volume through our price minus queue" model
# gets wrong -- 400 - 600 < 0 would report no fill.
sweep_700 = _tape((0.1583, 100, 'sell'), (0.1582, 200, 'sell'), (0.1581, 400, 'sell'))
check('a sweep past the queue ahead fills us',
      close(mb._fill_usd('bid', 0.1581, 50, 600, sweep_700), 50.0),
      str(mb._fill_usd('bid', 0.1581, 50, 600, sweep_700)))

check('a fill is never larger than the quote',
      close(mb._fill_usd('bid', 0.1581, 50, 0, sweep_700), 50.0))

check('volume on the wrong side of the book cannot fill a bid',
      close(mb._fill_usd('bid', 0.1581, 50, 0,
                         _tape((0.1581, 5000, 'buy'))), 0.0))

check('an ask is filled by aggressing buys',
      close(mb._fill_usd('ask', 0.1584, 50, 0,
                         _tape((0.1584, 500, 'buy'))), 50.0))

check('volume that never printed at our price cannot fill us',
      close(mb._fill_usd('ask', 0.1590, 50, 0,
                         _tape((0.1584, 5000, 'buy'))), 0.0),
      'a sweep that stopped below our ask must leave it resting')

check('partial fill is the leftover after the queue ahead',
      close(mb._fill_usd('bid', 0.1581, 50, 600,
                         _tape((0.1581, 620, 'sell'))), 20.0),
      str(mb._fill_usd('bid', 0.1581, 50, 600, _tape((0.1581, 620, 'sell')))))

check('unknown aggressor direction cannot fill anything',
      close(mb._fill_usd('bid', 0.1581, 50, 0,
                         [{'price': 0.1580, 'usd': 900, 'taker_side': None}]), 0.0))

check('a zero-size quote fills nothing', close(mb._fill_usd('bid', 0.1581, 0, 0, sweep_700), 0.0))

# ------------------------------------------------------------------- importability

check('quote() is the entry point, not decide()',
      mb.importability_report('def quote(book, state, config):\n    return None\n')[0])
check('decide() alone is not a maker',
      not mb.importability_report('def decide(p, h, s, c):\n    return None\n')[0])
check('a top-level side effect is rejected',
      not mb.importability_report(
          "import os\nos.system('x')\ndef quote(b, s, c):\n    return None\n")[0])
check('a __main__ guard is allowed',
      mb.importability_report(
          "def quote(b, s, c):\n    return None\n"
          "if __name__ == '__main__':\n    quote(None, None, None)\n")[0])

# ------------------------------------------------------------------- the null quoter

_null = mb.constant_width_quoter({'half_width_bp': 10.0, 'quote_usd': 50.0})
_q = _null({'mid': 0.1583}, {}, {})
check('null quotes symmetrically around the mid',
      close(_q['bid'][0], 0.1583 * 0.999) and close(_q['ask'][0], 0.1583 * 1.001),
      str(_q))
check('null stands down with no mid', _null({}, {}, {}) is None)
check('null never quotes inside the arithmetic floor',
      mb.constant_width_quoter({'half_width_bp': 0.0})(
          {'mid': 0.1583}, {}, {})['bid'][0] < 0.1583)

# -------------------------------------------------- the paper book charges the fill lag
# Fourth member of the silent-failure family in the module docstring, and the one that
# went unnoticed longest. quote_executor._paper_fills shares maker_backtest's MATCHER
# (_fill_usd) but the lag lives one level up, in _bucket_tape, which _paper_fills does not
# call. So the paper book ran at lag 0 while every backtest number it was compared against
# was charged 5 s -- and per MAKER_PHASE1.md's lag table that is 60% of the edge at a 5 bp
# half-width (+$5.08 vs +$2.02). Nothing crashed; the leaderboard simply reported an edge
# that a live account would not have earned.
#
# The window is [placed_ts + FILL_LAG_S, ...) per offer, so the fixtures below straddle
# that boundary at 4.9 s and 5.1 s rather than testing a comfortable midpoint.

import types

import quote_executor as _qe

_LAG_ROW = {'dex_mid': 0.155, 'dex_bid': 0.1549, 'dex_ask': 0.1551,
            'bid_cum': [[1, 0.0]], 'ask_cum': [[1, 0.0]]}
_LAG_T = 1_000_000.0


def _lag_fills(trade_offsets, placed_ts=_LAG_T):
    """Fills booked when the only tape is aggressor sells at these offsets from _LAG_T.

    Each trade is a $50 sell through a $20 bid resting at 0.1549 with an empty ladder in
    front of it, so absent the lag every one of them fills the quote outright -- the only
    thing that can hold a fill back here is the lag floor.
    """
    tape = [{'ts': _LAG_T + off, 'price': 0.1548, 'usd': 50.0, 'taker_side': 'sell'}
            for off in trade_offsets]
    real_tools, real_log = _qe._tools, _qe.record_trade
    _qe._tools = lambda: (types.SimpleNamespace(get_trades=lambda **kw: tape), mb, None)
    _qe.record_trade = lambda *a, **k: None   # keep the selftest out of /opt/trades
    try:
        state = _qe._normalize({'balance_usd': 1000.0, 'positions': {}, 'open_offers': [
            {'offer_id': None, 'side': 'bid', 'price': 0.1549, 'usd': 20.0,
             'amount_xlm': 129.1, 'placed_ts': placed_ts}]})
        state, n = _qe._paper_fills(state, '_selftest', _LAG_ROW, _LAG_T, _LAG_T + 30)
        return n, round(1000.0 - float(state['balance_usd']), 4)
    finally:
        _qe._tools, _qe.record_trade = real_tools, real_log


check('the paper book reads its lag from maker_backtest', mb.FILL_LAG_S == 5.0,
      str(mb.FILL_LAG_S))
check('a trade 1s after the quote was placed does not fill it',
      _lag_fills([1.0]) == (0, 0.0), str(_lag_fills([1.0])))
check('a trade just inside the lag (4.9s) does not fill',
      _lag_fills([4.9]) == (0, 0.0), str(_lag_fills([4.9])))
check('a trade just outside the lag (5.1s) does fill',
      _lag_fills([5.1]) == (1, 20.0), str(_lag_fills([5.1])))
# The pair matters on its own: _fill_usd sums volume_total across the tape it is handed,
# so a lag that filtered nothing would let the 1s trade help consume the queue ahead even
# if it could not fill directly. Both trades excluded means the tape itself was filtered.
check('a trade inside the lag is excluded from the tape, not just from the fill',
      _lag_fills([1.0, 20.0]) == (1, 20.0), str(_lag_fills([1.0, 20.0])))
check('an offer with no placed_ts still gets the lag charged from the window start',
      _lag_fills([2.0], placed_ts=0.0) == (0, 0.0),
      str(_lag_fills([2.0], placed_ts=0.0)))
check('an offer with no placed_ts fills past the window start plus the lag',
      _lag_fills([8.0], placed_ts=0.0) == (1, 20.0),
      str(_lag_fills([8.0], placed_ts=0.0)))

# ------------------------------------------------------- the daily cap is a real number
# MAKER.md's risk list: MAX_DAILY_USD sat at 99999.0 from `72fc3f4 TEMP` and a maker books
# far more fills per day than a taker. Pinned as "not the TEMP value" rather than as an
# exact figure -- the number is a human sizing decision (see the note beside it in
# stellar_trader.py) and should be re-sized as the caps move, but it must never go back to
# a value that means "no cap".
import stellar_trader as _st

check('MAX_DAILY_USD is not the TEMP placeholder', _st.MAX_DAILY_USD < 99999.0,
      str(_st.MAX_DAILY_USD))
check('MAX_DAILY_USD leaves room for a day of maker turnover at the resting cap',
      _st.MAX_DAILY_USD >= 100 * _st.MAX_RESTING_USD_PER_SIDE,
      f'{_st.MAX_DAILY_USD} vs {_st.MAX_RESTING_USD_PER_SIDE}')
check('the daily budget is reported against the same cap it enforces',
      _st.daily_spend_status()['cap_usd'] == _st.MAX_DAILY_USD)

# ------------------------------------------------------------------------------ done

if _failures:
    print(f'FAILED {len(_failures)} of {_passed + len(_failures)} checks:\n')
    for f in _failures:
        print(f'  - {f}')
    sys.exit(1)

print(f'ok - {_passed} checks passed')

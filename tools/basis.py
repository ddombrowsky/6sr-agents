#!/usr/bin/env python3
"""The gap between where XLM is quoted and where this system actually trades it.

Every strategy here decides on `price_feed.get_price()` -- an aggregate of Coinbase,
CoinGecko, Kraken, Bitstamp and Binance -- and then executes, when it executes for real,
as a path-payment against the Stellar DEX's own XLM/USDC book. Those are two different
venues with two different prices, and until now nothing measured the difference. That is
a blind spot in two directions at once:

  * It explains the paper/live divergence. The paper book fills at the CEX aggregate;
    the real order fills at whatever the Stellar book offers. Some of the gap
    live_report.py reports as "sizing" is actually venue.
  * It is a signal nothing is using. dex_price.get_mark('XLM') delegates straight to
    price_feed and get_mark_with_depth('XLM') returns an empty bid ladder, so XLM -- the
    dominant position in every strategy in the population -- is the one asset never
    priced against the book it trades on. dex_price.get_orderbook('XLM') has always
    worked; nobody called it.

A basis is a genuinely different input from price. Price says where the market is going;
this says whether, right now, this venue is the cheap or expensive place to act on that
view. A strategy that already wants to buy can wait for the DEX to be cheap relative to
the CEX rather than buying into a rich book, and that improvement is available without
predicting anything.

Measured 2026-08-03 for calibration: DEX mid 0.1713303 against a CEX aggregate of
0.171294, a basis of +3.6 bp, on a 12.6 bp book. Note the scale -- the basis is usually
a *fraction* of the spread, which is why `tradeable_bp` below is the only field worth
acting on: capturing a 3.6 bp dislocation by crossing a 12.6 bp book loses money. Expect
tradeable_bp to be negative most of the time. That is the honest answer, and a strategy
that trades only when it is positive is doing something the population currently cannot.

Read-only: two HTTP GETs, no keys, no signing, no relationship to the live-money path.

TWO ENTRY POINTS, and picking the wrong one is the failure mode this module has to
guard against. `get_basis()` measures the basis live and is for tooling, the CLI and
one-off inspection. `latest()` reads the basis market_recorder's daemon already
measured, does no network I/O at all, and is the ONLY one a strategy tick loop or
anything running per-candle may call. See each function's docstring.
"""
import time

QUOTE_SPREAD_SANITY = 0.05      # a book wider than 5% is not a quote worth basing on


def get_basis(spec='XLM', book=None):
    """DEX-vs-CEX dislocation for `spec`, or None if either side is unavailable.

    LIVE I/O -- one to two Horizon GETs per call, because dex_price.get_orderbook is not
    cached. This is a tooling and one-off-inspection entry point. Do NOT call it from a
    decide() step or any per-tick loop: ~10 running strategies on a 30s tick would be up
    to 40 order-book calls a minute, and under backtest it would fire once per replayed
    tick while answering every historical candle with today's number. `latest()` below
    is what a loop calls. Same warning applies to dex_is_cheap/dex_is_rich, which are
    each a fresh get_basis().

    Returns:
        cex_mid          reference price from price_feed (the CEX aggregate)
        dex_bid/ask/mid  the live Stellar DEX book
        spread_bp        width of that book
        basis_bp         (dex_mid - cex_mid) / cex_mid, in bp. Positive = the DEX is
                         rich, i.e. a good place to sell and a bad place to buy.
        tradeable_bp     basis_bp less the half-spread you would pay to capture it.
                         THIS is the actionable number; basis_bp alone is a mirage.
        bid_depth_usd / ask_depth_usd   how much size the dislocation is good for
        ts               when this was measured

    None on any failure rather than a partial dict: a caller gating a trade on this must
    not be handed a basis computed against a missing leg.
    """
    try:
        import dex_price
        import friction
        from price_feed import get_price
    except Exception:
        return None

    try:
        cex_mid = get_price() if spec == 'XLM' else dex_price.get_mark(spec)
    except Exception:
        cex_mid = None
    if not cex_mid or cex_mid <= 0:
        return None

    if book is None:
        try:
            book = dex_price.get_orderbook(spec)
        except Exception:
            book = None
    if not book or not book.get('mid') or book['mid'] <= 0:
        return None
    if book.get('spread_pct', 1.0) > QUOTE_SPREAD_SANITY:
        return None

    dex_mid = float(book['mid'])
    basis_bp = (dex_mid - cex_mid) / cex_mid * 10000
    # The width of THIS book, not of whatever book friction last cached. Without the
    # passthrough these two numbers -- which tradeable_bp subtracts from each other --
    # could come from books up to friction._CACHE_TTL apart.
    half_bp = friction.half_spread(spec, book) * 10000

    return {
        'spec': spec,
        'cex_mid': cex_mid,
        'dex_bid': book.get('best_bid'),
        'dex_ask': book.get('best_ask'),
        'dex_mid': dex_mid,
        'spread_bp': round(book['spread_pct'] * 10000, 2),
        'basis_bp': round(basis_bp, 2),
        # abs(): a dislocation is capturable in whichever direction it points, and the
        # cost of crossing is the same either way. The sign of basis_bp says which side
        # to be on; this says whether being on it is worth the toll.
        'tradeable_bp': round(abs(basis_bp) - half_bp, 2),
        'bid_depth_usd': book.get('bid_depth_usd'),
        'ask_depth_usd': book.get('ask_depth_usd'),
        'ts': time.time(),
    }


def latest(spec='XLM', max_age_s=180):
    """The most recent RECORDED basis for `spec`, or None if there isn't a usable one.

    What a 30s trading loop calls. Zero network I/O: it tails the single JSONL that
    market_recorder's daemon appends to once a minute, so cost is one small seek-read
    regardless of how many strategies are running or how long the file has grown.

    Never raises and NEVER falls back to a live fetch. The fallback is the tempting bug:
    it would turn one writer into N, and it would do so precisely when Horizon is
    already struggling, which is the moment the recorder went quiet. A None here means
    "the basis is unknown right now", and every caller must treat that as neutral rather
    than as a reading of zero -- zero is a real basis and a meaningfully different claim.

    max_age_s defaults to three recorder intervals, so a single missed row is tolerated
    and a dead recorder is not.
    """
    try:
        import market_recorder
        rows = market_recorder.tail(3)
    except Exception:
        return None
    now = time.time()
    for row in reversed(rows or []):
        try:
            if row.get('spec') != spec:
                continue
            if row.get('basis_bp') is None or not row.get('ts'):
                continue
            age = now - float(row['ts'])
            if age < 0 or age > max_age_s:
                continue
            return {
                'spec': spec,
                'basis_bp': row['basis_bp'],
                'tradeable_bp': row.get('tradeable_bp'),
                'spread_bp': row.get('spread_bp'),
                'ts': row['ts'],
                'age_s': round(age, 1),
                'src': 'recorded',
            }
        except Exception:
            continue
    return None


def dex_is_cheap(spec='XLM', min_bp=0.0):
    """True if the DEX is trading below the CEX by more than the cost of crossing.

    The buy-side gate, in the form a decide() step actually wants: "is this a good
    moment to be lifting this book at all?". False whenever the basis can't be measured
    -- an unmeasurable edge is not an edge.
    """
    b = get_basis(spec)
    return bool(b and b['basis_bp'] < 0 and b['tradeable_bp'] > min_bp)


def dex_is_rich(spec='XLM', min_bp=0.0):
    """True if the DEX is trading above the CEX by more than the cost of crossing.

    The sell-side mirror of dex_is_cheap.
    """
    b = get_basis(spec)
    return bool(b and b['basis_bp'] > 0 and b['tradeable_bp'] > min_bp)


if __name__ == '__main__':
    import json
    import sys
    result = get_basis(sys.argv[1] if len(sys.argv) > 1 else 'XLM')
    if not result:
        print('basis unavailable (one venue did not answer, or the book is too wide)')
        sys.exit(1)
    print(json.dumps(result, indent=2))
    verdict = ('DEX rich -- favours selling here' if result['basis_bp'] > 0
               else 'DEX cheap -- favours buying here')
    worth = 'worth crossing' if result['tradeable_bp'] > 0 else 'NOT worth crossing'
    print(f"\n{verdict}; {result['tradeable_bp']} bp after costs -- {worth}")

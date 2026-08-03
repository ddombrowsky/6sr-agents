#!/usr/bin/env python3
"""What a fill actually costs. The one place transaction cost is defined.

Until 2026-08-03 nothing in this system charged a spread, a fee, or slippage:
trade_logger.execute_trade bought at `price` and sold at the same `price`, and
backtest.py did the same. The fitness landscape was frictionless, so the evolutionary
loop selected for whatever churned hardest -- and churn is precisely what loses money on
a real DEX. The numbers that motivated this module, all measured on 2026-08-03:

  * The leader, clone_3b0033271b76: 962 trades in 87.1h (11.0/h), $5,757 of turnover,
    for a paper gain of $23.33. That is an edge of 40.5 bp per dollar traded -- thin
    enough that the cost of trading is a first-order term, not a rounding error.
  * The real XLM/USDC book on Horizon at the time: bid 0.171223 / ask 0.171438, a
    12.6 bp spread against $166k of bid depth. At ~6.3 bp per fill that is $3.63 of
    cost against a $23.33 gain -- about 16% of the entire edge, previously unmodelled.
  * The extra assets the population keeps trying to admit are far worse: AQUA's book
    was 148 bp wide and ARS's 195 bp (see /opt/trades/.verified_assets.json). A leg
    strategy churning at the XLM leg's cadence on a 148 bp book is not a marginal
    loser, it is arithmetically dead on arrival.
  * The live record already said so empirically: the same trades booked +0.06% on paper
    and +0.01% live-sized, because the paper book fills at CEX spot while the real order
    crosses the Stellar DEX book.

DESIGN RULE, and it is the opposite of monitor._importability_check's: this module
**fails toward charging**. If the book cannot be read, if Horizon is down, if the spec is
malformed -- the answer is the per-class floor, never zero. A network failure must never
make trading look free, because "free" is the assumption that produced a population of
churners in the first place. Nothing here raises, for the same reason dex_price doesn't:
it is called from inside a 30s trading loop and from the money path.

The half-spread is a deliberately conservative model of a taking order:
stellar_trader submits path-payment-strict-send swaps, which cross the book rather than
resting on it, so a round trip pays the full spread and each leg pays about half. Depth
is NOT modelled here -- walking the ladder for size is what dex_price.mark_value already
does for scoring, and at the $4-per-trade cap the top of book is never exhausted.
"""
import json
import time
from pathlib import Path

import assets

# Shared with dex_price's cache pattern, but a separate file: this TTL is much longer
# (a book's *width* is far more stable than its price) and mixing the two would mean
# either re-fetching spreads every 60s or serving stale prices for 5 minutes.
_CACHE_PATH = Path(__file__).resolve().parent / '.friction_cache.json'
_CACHE_TTL = 300

# Floors, used whenever the live book cannot be consulted. XLM's is the 2026-08-03
# measurement (12.6 bp spread -> 6.3 bp per side) rounded down slightly; the non-base
# floor is deliberately punitive because every non-XLM book measured so far has been
# an order of magnitude wider than XLM's, and a strategy that discovers it can trade a
# thin asset for free will do exactly that.
XLM_FLOOR = 0.0006          # 6 bp per fill
NONBASE_FLOOR = 0.0050      # 50 bp per fill

# Clamps on whatever the live book reports. The lower bound stops a momentarily crossed
# or one-sided book from pricing a fill at zero cost; the upper bound stops a single
# absurd quote (KES came back at a 200% spread this week) from making a leg's every
# trade a total loss and poisoning a backtest into nonsense.
MIN_HALF_SPREAD = 0.0002    # 2 bp
MAX_HALF_SPREAD = 0.0300    # 300 bp


def _cache_read(key):
    try:
        data = json.loads(_CACHE_PATH.read_text())
        entry = data.get(key)
        if entry and time.time() - entry['ts'] < _CACHE_TTL:
            return entry['value']
    except Exception:
        pass
    return None


def _cache_write(key, value):
    try:
        data = {}
        if _CACHE_PATH.exists():
            try:
                data = json.loads(_CACHE_PATH.read_text())
            except Exception:
                data = {}
        data[key] = {'ts': time.time(), 'value': value}
        cutoff = time.time() - _CACHE_TTL * 100
        data = {k: v for k, v in data.items() if v.get('ts', 0) > cutoff}
        tmp = _CACHE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(data))
        tmp.replace(_CACHE_PATH)   # atomic; strategies read this concurrently
    except Exception:
        pass


def _floor_for(spec):
    try:
        return XLM_FLOOR if assets.is_native(spec) else NONBASE_FLOOR
    except Exception:
        return NONBASE_FLOOR


def half_spread(spec='XLM'):
    """Cost of one fill in `spec`, as a fraction of price. Never raises, never zero.

    Read from the live order book (half of dex_price's spread_pct), clamped into
    [MIN_HALF_SPREAD, MAX_HALF_SPREAD], cached for _CACHE_TTL. Falls back to the
    per-class floor on any failure whatsoever -- see the module docstring on why the
    fallback charges rather than exempts.
    """
    try:
        spec = assets.normalize(spec)
    except Exception:
        return NONBASE_FLOOR

    cached = _cache_read(f'half_spread:{spec}')
    if cached is not None:
        return cached

    value = _floor_for(spec)
    try:
        import dex_price
        book = dex_price.get_orderbook(spec)
        raw = (book or {}).get('spread_pct')
        if raw is not None and raw == raw and raw > 0:
            value = min(MAX_HALF_SPREAD, max(MIN_HALF_SPREAD, float(raw) / 2.0))
    except Exception:
        pass    # value stays at the floor

    _cache_write(f'half_spread:{spec}', value)
    return value


def fill_price(spec, side, price, *, h=None):
    """The price a taking order in `spec` really fills at. Pure given `h`.

    buy  -> price * (1 + half_spread)   you lift the ask
    sell -> price * (1 - half_spread)   you hit the bid

    `price` is the reference (the CEX aggregate for XLM, the mark for anything else);
    the return is what execute_trade and backtest.py do their arithmetic with. Pass `h`
    to reuse a half-spread already fetched for a batch of fills -- backtest.py replays
    thousands of ticks and must not hit the cache for every one.

    A bad price is passed through untouched rather than being turned into a different
    bad number: execute_trade's own nan/zero/negative guard is upstream of this, and
    silently rewriting a value it is about to refuse would only obscure the refusal.
    """
    try:
        price = float(price)
        if price != price or price <= 0:
            return price
    except (TypeError, ValueError):
        return price
    if h is None:
        h = half_spread(spec)
    return price * (1.0 + h) if side == 'buy' else price * (1.0 - h)


def round_trip_bp(spec='XLM'):
    """Cost of a full buy-then-sell cycle in `spec`, in basis points.

    The number a strategy should be compared against: a threshold bot with a band
    narrower than this cannot be profitable no matter how often it is right.
    """
    return round(half_spread(spec) * 2 * 10000, 2)


def describe(spec='XLM'):
    h = half_spread(spec)
    return {
        'spec': spec,
        'half_spread': h,
        'half_spread_bp': round(h * 10000, 2),
        'round_trip_bp': round(h * 2 * 10000, 2),
        'is_floor': abs(h - _floor_for(spec)) < 1e-12,
    }


if __name__ == '__main__':
    import sys
    specs = sys.argv[1:] or ['XLM']
    for s in specs:
        d = describe(s)
        print(f"{d['spec']}: {d['half_spread_bp']} bp per fill, "
              f"{d['round_trip_bp']} bp round trip"
              f"{'  (floor -- book unavailable)' if d['is_floor'] else ''}")

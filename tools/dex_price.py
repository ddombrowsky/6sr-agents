#!/usr/bin/env python3
"""Prices for Stellar-DEX-only assets, and the conservative mark used for scoring.

price_feed.get_price() prices XLM by polling five centralized exchanges. None of them
list an arbitrary Stellar asset, so anything other than XLM has to be priced from
Stellar's own on-chain DEX (Horizon) or from the Reflector oracle. That is what this
module does, and it is the only place non-XLM prices come from.

## Why marks are bid-side and depth-capped

Paper net worth decides which strategies get cloned. If a held asset were marked at the
mid price, the ranking would be trivially gameable: pick a token with a $5 order book,
"buy" 100,000 units of it in paper, and a single stale 1-unit ask marks the whole
position at ten times what it could ever be sold for. The strategy wins the leaderboard
by holding something worthless.

So `mark_value(spec, amount)` walks the *bid* side and returns what the position could
actually be sold into right now. Quantity beyond available bid depth marks at **zero**,
not at the last price. A mark is an exit estimate, not a quote.

## Horizon order book denominations (verified live, not from docs)

Confirmed by fetching the same book with base and counter swapped and matching the
identical offer on both sides:

    bids: `amount` is in COUNTER units   (for base=XLM/counter=USDC, amount is USDC)
    asks: `amount` is in BASE units      (amount is XLM)

so a bid's USD value is `amount` directly, and an ask's is `price * amount`. This is
easy to get backwards -- tools/orderbook_depth.py multiplies bid amounts by price, which
undercounts bid depth by a factor of the price (measured: $0.34 reported against $1.96
actual on the XLM/USDC book) and biases its `imbalance` toward "ask-heavy". Do not copy
that pattern from there.

## Failure behavior

Every function returns None (or an empty list) rather than raising, and never falls back
to a less trustworthy number to avoid returning None. A price that cannot be established
is reported as absent so the caller can treat the position as unpriced -- an asset that
suddenly cannot be priced is what a rug looks like, and substituting a stale or
optimistic figure would hide exactly the event worth reacting to.
"""
import json
import time
from pathlib import Path

import requests

import assets

_HORIZON = 'https://horizon.stellar.org'
_TIMEOUT = 10

# Shared on-disk cache. ~40 strategies x up to 3 legs x a 30s tick would hammer public
# Horizon into rate-limiting within minutes, so every non-XLM read goes through here.
# Same pattern as .ohlc_cache_60.json / .price_history_cache.json.
_CACHE_PATH = Path(__file__).resolve().parent / '.asset_price_cache.json'
_CACHE_TTL = 60

# A book wider than this is not a price, it's a suggestion.
_MAX_SPREAD_PCT = 0.03
_ORDERBOOK_DEPTH = 50


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
        # Drop entries far past their TTL so the file can't grow without bound as the
        # discovered-asset universe rotates.
        cutoff = time.time() - _CACHE_TTL * 100
        data = {k: v for k, v in data.items() if v.get('ts', 0) > cutoff}
        tmp = _CACHE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(data))
        tmp.replace(_CACHE_PATH)   # atomic; strategies read this concurrently
    except Exception:
        pass


class _NoMarket(Exception):
    """Horizon rejected the asset outright -- it has no market here. Expected, not an
    error: an unknown or never-traded (code, issuer) is exactly what a fabricated or
    impersonated asset looks like, and this is called from a 30s trading loop, so it
    must not spew tracebacks."""


def _get(path, params):
    resp = requests.get(f'{_HORIZON}{path}', params=params, timeout=_TIMEOUT)
    if resp.status_code == 400:
        raise _NoMarket(path)
    resp.raise_for_status()
    return resp.json()


def _align(ms, resolution):
    """Floor a millisecond timestamp to a resolution boundary.

    Horizon's /trade_aggregations only returns buckets whose boundaries fall inside
    [start_time, end_time]. An unaligned one-hour window can therefore straddle a single
    bucket and match none of it, silently returning zero records for an asset that is
    actively trading -- which reads as "no price" and demotes the whole source.
    """
    return (ms // resolution) * resolution


def _quote_usd_rate(quote):
    """USD value of one unit of the quote asset, or None.

    USDC is treated as $1. Quoting in XLM requires the live XLM/USD rate, which is what
    lets an asset that only trades against XLM still be priced in USD.
    """
    if assets.is_native(quote):
        from price_feed import get_price
        return get_price()
    return 1.0 if assets.normalize(quote) == assets.usdc() else None


def _quotes_for(spec):
    """Quote assets to try for `spec`, best first.

    USDC first (a direct USD price, no conversion error), then XLM -- many Stellar
    assets have a deeper book against XLM than against USDC, and an asset that only
    trades against XLM would otherwise look unpriceable. An asset is never quoted
    against itself, which is what made USDC report "no order book against USDC".
    """
    spec = assets.normalize(spec)
    out = []
    if spec != assets.usdc():
        out.append(assets.usdc())
    if not assets.is_native(spec):
        out.append(assets.NATIVE)
    return out


def get_orderbook(spec, quote=None, depth=_ORDERBOOK_DEPTH):
    """Normalized order book for `spec` priced in `quote` (default USDC).

    Returns, or None on any failure / empty side:
        {'bids': [{'price', 'base_amount', 'usd'}, ...],   # descending price
         'asks': [{'price', 'base_amount', 'usd'}, ...],   # ascending price
         'best_bid', 'best_ask', 'mid', 'spread_pct',
         'bid_depth_usd', 'ask_depth_usd'}

    `base_amount` and `usd` are normalized per the denomination rule in the module
    docstring, so callers never have to think about which side they're on. All prices
    and depths are returned in **USD**, converted from the quote asset if needed, so a
    book quoted in XLM is directly comparable with one quoted in USDC.

    With `quote` unset, quotes are tried best-first (USDC, then XLM) and the first book
    that exists wins.
    """
    if quote is None:
        for candidate in _quotes_for(spec):
            book = get_orderbook(spec, quote=candidate, depth=depth)
            if book is not None:
                return book
        return None

    rate = _quote_usd_rate(quote)
    if rate is None or rate <= 0:
        return None
    try:
        params = {'limit': depth}
        params.update(assets.horizon_params(spec, 'selling_asset'))
        params.update(assets.horizon_params(quote, 'buying_asset'))
        data = _get('/order_book', params)
    except _NoMarket:
        return None
    except Exception as e:
        print(f'[dex_price] order book {assets.display(spec)}: {e}')
        return None

    raw_bids, raw_asks = data.get('bids') or [], data.get('asks') or []
    if not raw_bids or not raw_asks:
        return None

    bids, asks = [], []
    try:
        for b in raw_bids:
            price, amount = float(b['price']), float(b['amount'])
            if price <= 0 or amount <= 0:
                continue
            # bid `amount` is already in counter units -- do NOT multiply by price
            bids.append({'price': price * rate, 'usd': amount * rate,
                         'base_amount': amount / price})
        for a in raw_asks:
            price, amount = float(a['price']), float(a['amount'])
            if price <= 0 or amount <= 0:
                continue
            # ask `amount` is in base units -- value needs the price
            asks.append({'price': price * rate, 'usd': price * amount * rate,
                         'base_amount': amount})
    except (TypeError, ValueError, KeyError):
        return None

    if not bids or not asks:
        return None

    best_bid, best_ask = bids[0]['price'], asks[0]['price']
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return None

    return {
        'bids': bids, 'asks': asks,
        'best_bid': best_bid, 'best_ask': best_ask, 'mid': mid,
        'spread_pct': (best_ask - best_bid) / mid,
        'bid_depth_usd': sum(b['usd'] for b in bids),
        'ask_depth_usd': sum(a['usd'] for a in asks),
    }


def get_vwap(spec, quote=None, hours=1):
    """Volume-weighted average price over the last `hours`, or None.

    None unless there were actual trades in the window: `avg` on an empty bucket is
    meaningless, and a price nobody traded at is not evidence the asset is tradeable.
    """
    if quote is None:
        for candidate in _quotes_for(spec):
            vwap = get_vwap(spec, quote=candidate, hours=hours)
            if vwap is not None:
                return vwap
        return None

    rate = _quote_usd_rate(quote)
    if rate is None or rate <= 0:
        return None
    resolution = 3600000
    now_ms = int(time.time() * 1000)
    # Both bounds aligned, and start pulled back one extra bucket: the current hour is
    # partial, so without the extra bucket a freshly-started hour yields no closed
    # bucket and the source reports "no price" for an actively-traded asset.
    end_ms = _align(now_ms, resolution) + resolution
    start_ms = _align(now_ms - hours * resolution, resolution) - resolution
    try:
        params = {
            'resolution': resolution,
            'start_time': start_ms,
            'end_time': end_ms,
            'order': 'desc', 'limit': max(2, hours + 1),
        }
        params.update(assets.horizon_params(spec, 'base_asset'))
        params.update(assets.horizon_params(quote, 'counter_asset'))
        records = _get('/trade_aggregations', params).get('_embedded', {}).get('records', [])
    except _NoMarket:
        return None
    except Exception as e:
        print(f'[dex_price] vwap {assets.display(spec)}: {e}')
        return None

    base_vol = counter_vol = 0.0
    for r in records:
        try:
            if int(r.get('trade_count', 0)) <= 0:
                continue
            base_vol += float(r['base_volume'])
            counter_vol += float(r['counter_volume'])
        except (TypeError, ValueError, KeyError):
            continue
    if base_vol <= 0 or counter_vol <= 0:
        return None
    return (counter_vol / base_vol) * rate


def get_candles(spec, quote=None, hours=168, resolution=3600000):
    """Hourly OHLC history, newest last. [] on failure.

    The DEX equivalent of ohlc_history.py (which is XLM-only via Kraken/Coinbase).
    History here is thinner and gappier than a CEX feed -- buckets with no trades are
    simply absent rather than zero-filled -- so treat it as indicative.
    """
    if quote is None:
        for candidate in _quotes_for(spec):
            candles = get_candles(spec, quote=candidate, hours=hours, resolution=resolution)
            if candles:
                return candles
        return []

    now_ms = int(time.time() * 1000)
    try:
        params = {
            'resolution': resolution,
            'start_time': _align(now_ms - hours * 3600000, resolution),
            'end_time': _align(now_ms, resolution) + resolution,
            'order': 'desc', 'limit': 200,
        }
        params.update(assets.horizon_params(spec, 'base_asset'))
        params.update(assets.horizon_params(quote, 'counter_asset'))
        records = _get('/trade_aggregations', params).get('_embedded', {}).get('records', [])
    except _NoMarket:
        return []
    except Exception as e:
        print(f'[dex_price] candles {assets.display(spec)}: {e}')
        return []

    out = []
    for r in reversed(records):
        try:
            out.append({
                'timestamp': int(r['timestamp']) // 1000,
                'open': float(r['open']), 'high': float(r['high']),
                'low': float(r['low']), 'close': float(r['close']),
                'base_volume': float(r['base_volume']),
                'counter_volume': float(r['counter_volume']),
                'trade_count': int(r['trade_count']),
            })
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _reflector_mark(spec):
    """Reflector oracle price, or None. Tried first: it is a curated feed quoted in USDC
    and far harder to manipulate than a thin book. Requires the `stellar` CLI to resolve
    the asset's contract id, so it is expected to be unavailable in some environments --
    that is not an error, just a missing source.
    """
    code, issuer = assets.parse(spec)
    if issuer is None:
        return None
    try:
        import subprocess

        import reflector_oracle
        key = f'sac:{spec}'
        sac = _cache_read(key)
        if sac is None:
            result = subprocess.run(
                ['stellar', 'contract', 'id', 'asset', '--asset', assets.sep11(spec),
                 '--network', 'pubnet'],
                capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return None
            sac = result.stdout.strip()
            if sac:
                _cache_write(key, sac)   # deterministic; safe to cache
        return reflector_oracle.get_price(sac) if sac else None
    except Exception:
        return None


def get_mark(spec, quote=None):
    """Conservative unit price in USD for one unit of `spec`, or None.

    XLM delegates to price_feed.get_price() unchanged. For everything else the sources
    are tried in decreasing order of trustworthiness and the first usable one wins:

      1. Reflector oracle, if it tracks the asset
      2. 1h VWAP, if anything actually traded
      3. best *bid* -- never the mid, and only if the spread is tight enough

    Cached for 60s. Use mark_value() to value a held position; this is a unit price and
    deliberately ignores size.
    """
    try:
        spec = assets.normalize(spec)
    except assets.AssetError:
        return None

    if assets.is_native(spec):
        from price_feed import get_price
        return get_price()

    cached = _cache_read(f'mark:{spec}')
    if cached is not None:
        return cached

    mark = _reflector_mark(spec)
    if mark is None:
        mark = get_vwap(spec, quote=quote, hours=1)
    if mark is None:
        book = get_orderbook(spec, quote=quote)
        if book and book['spread_pct'] <= _MAX_SPREAD_PCT:
            mark = book['best_bid']

    if mark is not None and mark > 0:
        _cache_write(f'mark:{spec}', mark)
        return mark
    return None


def realize_against_bids(amount, bids):
    """USD obtainable for `amount` units by walking a bid ladder. Pure, no network.

    Split out so scoring can depth-cap every strategy's position from ONE order book
    fetched per asset per cycle, instead of hitting Horizon per strategy. Quantity beyond
    the available bids contributes nothing -- there is nobody to sell it to.
    """
    remaining, proceeds = float(amount), 0.0
    for bid in bids or []:
        if remaining <= 0:
            break
        take = min(remaining, float(bid['base_amount']))
        proceeds += take * float(bid['price'])
        remaining -= take
    return proceeds


def get_mark_with_depth(spec, quote=None):
    """{'price': float, 'bids': [...]} for `spec`, or None.

    The bid ladder is USD-normalized by get_orderbook, so callers can walk it directly.
    Fetched once per asset per scoring cycle and reused across every strategy holding it.
    """
    price = get_mark(spec, quote=quote)
    if price is None:
        return None
    book = None if assets.is_native(spec) else get_orderbook(spec, quote=quote)
    return {'price': price, 'bids': (book or {}).get('bids', [])}


def mark_value(spec, amount, quote=None):
    """USD the position `amount` of `spec` could actually be sold into right now.

    This is the figure scoring should rank on. It walks the real bid ladder and stops
    when the bids run out: any quantity beyond available bid depth is worth **zero**,
    because there is nobody to sell it to. That makes the "buy a huge position in an
    illiquid token and get marked at the mid" attack unprofitable rather than optimal.

    Returns None if the asset cannot be priced at all (caller should treat the leg as
    unpriced), and 0.0 for a real position with no bids under it -- those are different
    facts and must not be conflated.
    """
    try:
        spec = assets.normalize(spec)
        amount = float(amount)
    except (assets.AssetError, TypeError, ValueError):
        return None
    if amount <= 0:
        return 0.0

    # XLM is deep on real exchanges; the on-chain book is not where it would be sold,
    # so depth-capping it against Horizon would understate it badly.
    if assets.is_native(spec):
        price = get_mark(spec)
        return None if price is None else amount * price

    book = get_orderbook(spec, quote=quote)
    if book is None:
        price = get_mark(spec, quote=quote)
        return None if price is None else amount * price

    # unfilled remainder contributes nothing, by design
    return realize_against_bids(amount, book['bids'])


def get_marks(specs, quote=None):
    """Batch unit marks: {spec: price|None}. One cache pass, for monitor's scoring."""
    return {spec: get_mark(spec, quote=quote) for spec in dict.fromkeys(specs)}


if __name__ == '__main__':
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else 'XLM'
    print(f'{target}:')
    print(f'  mark           {get_mark(target)}')
    print(f'  vwap(1h)       {get_vwap(target) if target != "XLM" else "n/a"}')
    book = get_orderbook(target) if target != 'XLM' else None
    if book:
        print(f'  best_bid/ask   {book["best_bid"]} / {book["best_ask"]}')
        print(f'  spread_pct     {book["spread_pct"]:.4%}')
        print(f'  bid/ask depth  ${book["bid_depth_usd"]:.2f} / ${book["ask_depth_usd"]:.2f}')

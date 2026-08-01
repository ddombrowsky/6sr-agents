#!/usr/bin/env python3
"""Real historical XLM/USD candles (OHLCV), keyless, shared and cached.

Why this exists separately from price_history_fetcher.py: that module builds a
rolling buffer of *live* spot samples (500 max, one every 120s), so a freshly
cloned strategy starts with essentially no history and any indicator it wires in
(EMA/SMA/RSI) returns nan for its first several hours. This module fetches real
historical candles from an exchange instead, so a brand-new strategy has ~30 days
of usable lookback the first time it runs, and so backtest.py has something to
replay against.

Sources (both keyless, verified working 2026-08-01):
  * Kraken     /0/public/OHLC          -- up to 720 candles per request
  * Coinbase   /products/XLM-USD/candles -- up to 300 candles per request (fallback)

Typical use from a strategy's main.py:

    import sys; sys.path.append('/opt/tools')
    from ohlc_history import closes
    from moving_averages import exponential_moving_average
    ema = exponential_moving_average(closes(hours=168), 24)
"""
import json
import os
import time

import requests

_TIMEOUT = 15
CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
REFRESH_INTERVAL = 300  # seconds; candles only close every `interval` minutes anyway

# Kraken returns its own asset-pair naming (XXLMZUSD) rather than the pair you asked
# for, and the key isn't documented as stable, so we take whichever key is present.
_KRAKEN_URL = 'https://api.kraken.com/0/public/OHLC'
_COINBASE_URL = 'https://api.exchange.coinbase.com/products/XLM-USD/candles'


def _cache_path(interval):
    return os.path.join(CACHE_DIR, f'.ohlc_cache_{interval}.json')


def _load_cache(interval):
    path = _cache_path(interval)
    if not os.path.exists(path):
        return None, 0
    try:
        with open(path) as f:
            blob = json.load(f)
        return blob.get('candles') or None, blob.get('fetched_at', 0)
    except Exception:
        return None, 0


def _save_cache(interval, candles):
    try:
        with open(_cache_path(interval), 'w') as f:
            json.dump({'fetched_at': int(time.time()), 'candles': candles}, f)
    except Exception as e:
        print(f'[ohlc_history] could not save cache: {e}')


def _from_kraken(interval):
    resp = requests.get(
        _KRAKEN_URL, params={'pair': 'XLMUSD', 'interval': interval}, timeout=_TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get('error'):
        raise RuntimeError(data['error'])
    result = data['result']
    rows = next(v for k, v in result.items() if k != 'last')
    # row: [time, open, high, low, close, vwap, volume, count]
    return [
        {
            'ts': int(r[0]),
            'open': float(r[1]),
            'high': float(r[2]),
            'low': float(r[3]),
            'close': float(r[4]),
            'volume': float(r[6]),
        }
        for r in rows
    ]


def _from_coinbase(interval):
    resp = requests.get(
        _COINBASE_URL, params={'granularity': interval * 60}, timeout=_TIMEOUT
    )
    resp.raise_for_status()
    # row: [time, low, high, open, close, volume], newest first
    rows = resp.json()
    candles = [
        {
            'ts': int(r[0]),
            'open': float(r[3]),
            'high': float(r[2]),
            'low': float(r[1]),
            'close': float(r[4]),
            'volume': float(r[5]),
        }
        for r in rows
    ]
    candles.sort(key=lambda c: c['ts'])
    return candles


_SOURCES = [('kraken', _from_kraken), ('coinbase', _from_coinbase)]


def get_candles(hours=720, interval=60):
    """Return up to `hours` worth of candles, oldest first.

    `interval` is the candle size in minutes (1, 5, 15, 60, 240, 1440 -- values both
    sources accept). Results are cached on disk per interval and reused for
    REFRESH_INTERVAL seconds, so many strategies calling this in the same cycle share
    one upstream request instead of each hitting the API (the rate-limit failure mode
    a prior emperor pass logged for price_feed).

    Returns [] if every source fails -- callers should treat that the same way they
    treat price_feed.get_price() returning None, not as "the market is flat".
    """
    cached, fetched_at = _load_cache(interval)
    if cached and time.time() - fetched_at < REFRESH_INTERVAL:
        candles = cached
    else:
        candles = None
        for name, fn in _SOURCES:
            try:
                candles = fn(interval)
                if candles:
                    _save_cache(interval, candles)
                    break
            except Exception as e:
                print(f'[ohlc_history] {name} error: {e}')
        if not candles:
            if cached:
                print('[ohlc_history] all sources failed; serving stale cache')
                candles = cached
            else:
                print('[ohlc_history] all sources failed and no cache available')
                return []

    wanted = max(1, int(hours * 60 / interval))
    return candles[-wanted:]


def closes(hours=720, interval=60):
    """Just the close prices, oldest first -- drops straight into the indicator
    modules in this directory (ema_sma.sma, moving_averages.exponential_moving_average,
    rsi.rsi), all of which take a plain list of prices."""
    return [c['close'] for c in get_candles(hours=hours, interval=interval)]


if __name__ == '__main__':
    candles = get_candles()
    if not candles:
        print('no candles')
    else:
        print(f'{len(candles)} candles, {candles[0]["ts"]} .. {candles[-1]["ts"]}')
        print(f'oldest close ${candles[0]["close"]:.6f}, newest close ${candles[-1]["close"]:.6f}')

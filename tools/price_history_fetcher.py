#!/usr/bin/env python3
"""Cached XLM/USD price history, shared across strategies.

get_price_samples() maintains a small on-disk cache of recent price samples,
refreshed via the shared price_feed.get_price() (5-source fallback chain) at
most once every REFRESH_INTERVAL seconds. Many strategies calling this in the
same cycle then share a handful of upstream API calls instead of each hitting
the price APIs independently every 30s, which is what caused the rate-limit
failures a prior emperor pass logged.
"""
import json
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from price_feed import get_price

MAX_SAMPLES = 500
REFRESH_INTERVAL = 120  # seconds
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.price_history_cache.json')


def _load_cache():
    if not os.path.exists(CACHE_FILE):
        return []
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_cache(samples):
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(samples[-MAX_SAMPLES:], f)
    except Exception as e:
        print(f"[price_history_fetcher] could not save cache: {e}")


def get_price_samples(agent_name: str | None = None, lookback_cycles: int = 20):
    """Return up to `lookback_cycles` recent XLM/USD prices, oldest first.

    `agent_name` is accepted for call-site compatibility but all callers
    share the same on-disk cache, which is the point: one shared history
    instead of each strategy fetching (and rate-limiting) independently.
    """
    samples = _load_cache()
    now = int(time.time())
    last_ts = samples[-1]['timestamp'] if samples else 0

    if now - last_ts > REFRESH_INTERVAL:
        price = get_price()
        if price is not None:
            samples.append({'timestamp': now, 'price': price})
            samples = samples[-MAX_SAMPLES:]
            _save_cache(samples)

    recent = samples[-lookback_cycles:] if lookback_cycles else samples
    return [s['price'] for s in recent]


if __name__ == '__main__':
    print(get_price_samples())

#!/usr/bin/env python3
"""Price feed for Stellar (XLM), USD.
Provides a single function ``get_price()`` returning the current USD price as a float.

Tries multiple keyless sources in order and returns the first that succeeds, so a
single API outage/rate-limit doesn't take down the trading loop. Each source has its
own response shape, so each gets its own small parser.

## Why the result is cached on disk, not in-process

Every strategy is its own `main.py` process on a 30s tick, and each one called
straight through to Coinbase -- so the fleet's request rate was (strategies x 2)/min
from a single IP, scaling with the population rather than with anything about the
market. At 22 strategies the run logs already showed CoinGecko returning 429 and ten
"all sources failed" ticks. An in-process cache fixes nothing here: the processes are
separate, so the file *is* the shared state. Same pattern as
dex_price.py's .asset_price_cache.json.

This matters beyond paper trading: the live strategy prices its real pubnet orders
through this function, and monitor's per-cycle fetch sleeps 300s and burns the cycle
when it comes back None. Both share whatever rate-limit budget the paper strategies
leave behind.

Failures are cached too, for a shorter _ERROR_TTL: a rate-limit storm is exactly when
every source fails, and 22 processes each retrying six sources -- the last of which is
a ~20s reflector CLI subprocess -- is the worst possible response to being throttled.
_ERROR_TTL is deliberately shorter than monitor's PRICE_RETRY_DELAY (60s) so that its
retry loop always re-fetches instead of being handed back its own cached failure.
"""
import json
import math
import os
import time
from pathlib import Path

import requests

_TIMEOUT = 10

# Shared on-disk cache; see the module docstring.
_CACHE_PATH = Path(__file__).resolve().parent / '.xlm_price_cache.json'
_CACHE_TTL = 60
_ERROR_TTL = 20
_CACHE_KEY = 'xlm_usd'


def _cache_read(max_age):
    """Cached (price, True) if still fresh, else (None, False).

    A cached failure is a stored value of None and expires after _ERROR_TTL rather
    than max_age. The second element distinguishes "cached failure" from "no entry",
    which a bare None return could not.
    """
    try:
        entry = json.loads(_CACHE_PATH.read_text()).get(_CACHE_KEY)
        if entry:
            ttl = max_age if entry['value'] is not None else min(max_age, _ERROR_TTL)
            if time.time() - entry['ts'] < ttl:
                return entry['value'], True
    except Exception:
        pass
    return None, False


def _cache_write(price):
    try:
        # Unique tmp name per process: ~22 strategies write this concurrently, and a
        # shared tmp path lets one writer's replace() publish another's half-written
        # file. replace() itself is atomic, so readers never see a partial file.
        tmp = _CACHE_PATH.with_suffix(f'.tmp.{os.getpid()}')
        tmp.write_text(json.dumps({_CACHE_KEY: {'ts': time.time(), 'value': price}}))
        tmp.replace(_CACHE_PATH)
    except Exception:
        pass


def _from_coingecko():
    resp = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "stellar", "vs_currencies": "usd"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return float(resp.json()["stellar"]["usd"])


def _from_coinbase():
    resp = requests.get("https://api.coinbase.com/v2/prices/XLM-USD/spot", timeout=_TIMEOUT)
    resp.raise_for_status()
    return float(resp.json()["data"]["amount"])


def _from_kraken():
    resp = requests.get(
        "https://api.kraken.com/0/public/Ticker",
        params={"pair": "XLMUSD"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    result = data["result"]
    pair_data = next(iter(result.values()))
    return float(pair_data["c"][0])  # last trade closeout price


def _from_bitstamp():
    resp = requests.get("https://www.bitstamp.net/api/v2/ticker/xlmusd/", timeout=_TIMEOUT)
    resp.raise_for_status()
    return float(resp.json()["last"])


def _from_binance():
    resp = requests.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": "XLMUSDT"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return float(resp.json()["price"])


def _from_reflector():
    # On-chain Soroban oracle, not subject to the centralized-exchange REST
    # rate limits above. Slower (~20s CLI subprocess call, 5-min tick
    # resolution) so it's kept last: a fallback for when all REST sources
    # are simultaneously rate-limited/down, not a primary source.
    import reflector_oracle
    price = reflector_oracle.get_price()
    if price is None:
        raise RuntimeError("reflector oracle returned no price")
    return price


# Order matters: earlier sources are preferred, later ones are fallbacks.
_SOURCES = [
    ("coinbase", _from_coinbase),
    ("coingecko", _from_coingecko),
    ("kraken", _from_kraken),
    ("bitstamp", _from_bitstamp),
    ("binance", _from_binance),
    ("reflector", _from_reflector),
]


def get_price(max_age=_CACHE_TTL):
    """Current XLM/USD, or None if every source failed.

    Served from the shared on-disk cache when the last result is younger than
    `max_age` seconds. Pass max_age=0 to force a live fetch.
    """
    price, hit = _cache_read(max_age)
    if hit:
        return price

    for name, fetch in _SOURCES:
        try:
            price = fetch()
            # Only a sane number is worth pinning into shared state for the whole TTL.
            # A source that hands back nan/0 is returned to this caller exactly as it
            # always was -- trade_logger refuses those -- but caching it would serve
            # the same bad number to every strategy for the next minute.
            if isinstance(price, (int, float)) and math.isfinite(price) and price > 0:
                _cache_write(price)
            return price
        except Exception as e:
            print(f"[price_feed] {name} error: {e}")
    print("[price_feed] all sources failed")
    _cache_write(None)
    return None

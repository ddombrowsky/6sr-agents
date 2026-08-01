#!/usr/bin/env python3
"""Price feed for Stellar (XLM), USD.
Provides a single function ``get_price()`` returning the current USD price as a float.

Tries multiple keyless sources in order and returns the first that succeeds, so a
single API outage/rate-limit doesn't take down the trading loop. Each source has its
own response shape, so each gets its own small parser.

"""
import requests

_TIMEOUT = 10


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


def get_price():
    for name, fetch in _SOURCES:
        try:
            return fetch()
        except Exception as e:
            print(f"[price_feed] {name} error: {e}")
    print("[price_feed] all sources failed")
    return None

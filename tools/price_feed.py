#!/usr/bin/env python3
"""Simple price feed for Stellar (XLM) using CoinGecko API.
Provides a single function ``get_price()`` returning the current USD price as a float.
Rate limiting: CoinGecko free tier allows 50 calls/minute; callers should space out requests.
"""
import requests

_API_URL = "https://api.coingecko.com/api/v3/simple/price"
_PARAMS = {"ids": "stellar", "vs_currencies": "usd"}

def get_price():
    try:
        resp = requests.get(_API_URL, params=_PARAMS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return float(data["stellar"]["usd"])
    except Exception as e:
        print(f"[price_feed] error fetching price: {e}")
        return None

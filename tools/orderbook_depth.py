#!/usr/bin/env python3
"""XLM/USDC order book depth and spread from Stellar's on-chain DEX (Horizon).

This is a genuinely different signal from anything else in /opt/tools: price_feed.py
and reflector_oracle.py give a single spot price, aggregated or oracle-derived, but
neither says anything about how *liquid* the market is right now or which side of the
book is currently heavier. A widening spread or a lopsided book (much more resting
supply than demand, or vice versa) can be a useful gate for trade timing/sizing --
e.g. skip or shrink a trade when the spread is unusually wide (illiquid, high slippage
risk) or lean into a trade when the book is heavily stacked on your side.

Uses the same public USDC issuer as tools/stellar_trader.py's live trading (Circle's
well-known pubnet USDC), but this module is read-only -- a single keyless GET to
Horizon's public order_book endpoint, no `stellar` CLI, no signing, no relation to the
live-trading safety boundary in stellar_trader.py. Safe to call from any strategy's
main.py.
"""
import requests

_HORIZON = "https://horizon.stellar.org"
_TIMEOUT = 10
_USDC_ISSUER = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"


def get_orderbook_metrics(depth=20):
    """Fetch the live XLM/USDC order book and summarize it.

    Returns a dict:
      {best_bid, best_ask, mid_price, spread, spread_pct,
       bid_depth_usd, ask_depth_usd, imbalance}
    or None on any failure (network error, empty book, unexpected response shape) --
    callers should treat None as "no liquidity signal available right now", the same
    convention as price_feed.get_price() and news_feed.get_headlines().

    bid_depth_usd / ask_depth_usd: total USDC notional resting in the top `depth`
    levels on each side. imbalance: (bid_depth_usd - ask_depth_usd) / (bid_depth_usd +
    ask_depth_usd), in [-1, 1] -- positive means more resting demand than supply.
    """
    params = {
        "selling_asset_type": "native",
        "buying_asset_type": "credit_alphanum4",
        "buying_asset_code": "USDC",
        "buying_asset_issuer": _USDC_ISSUER,
        "limit": depth,
    }
    try:
        resp = requests.get(f"{_HORIZON}/order_book", params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return None

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        mid_price = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

        bid_depth_usd = sum(float(b["price"]) * float(b["amount"]) for b in bids)
        ask_depth_usd = sum(float(a["price"]) * float(a["amount"]) for a in asks)
        total_depth = bid_depth_usd + ask_depth_usd
        imbalance = (bid_depth_usd - ask_depth_usd) / total_depth if total_depth else 0.0

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid_price,
            "spread": spread,
            "spread_pct": spread / mid_price if mid_price else 0.0,
            "bid_depth_usd": bid_depth_usd,
            "ask_depth_usd": ask_depth_usd,
            "imbalance": imbalance,
        }
    except Exception as e:
        print(f"[orderbook_depth] error fetching order book: {e}")
        return None


if __name__ == "__main__":
    metrics = get_orderbook_metrics()
    if metrics is None:
        print("could not fetch order book")
    else:
        for k, v in metrics.items():
            print(f"  {k}: {v}")

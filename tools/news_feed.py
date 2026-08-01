#!/usr/bin/env python3
"""Crypto/XLM news headlines plus a lightweight keyword-based sentiment score.

This is not real NLP -- it's a cheap keyword-counting heuristic, so strategies
have *some* non-price signal to combine with technical indicators, without
adding any new installed dependencies or requiring an API key. Source is
Cointelegraph's public per-tag RSS feed (keyless, no rate-limit tier to
manage) rather than a JSON news API -- CryptoCompare's `/data/v2/news/`
endpoint (used by an earlier version of this file) now returns 401
Unauthorized without an API key, so it was dropped. Treat sentiment_score()
as a rough directional hint, not a reliable prediction.
"""
import xml.etree.ElementTree as ET

import requests

_TIMEOUT = 10
_RSS_URL_TEMPLATE = "https://cointelegraph.com/rss/tag/{tag}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; xlm-trading-bot/1.0)"}
# Cointelegraph's per-tag RSS slugs aren't always the ticker itself.
_ASSET_TAGS = {"XLM": "stellar", "STELLAR": "stellar"}

_BULLISH_WORDS = [
    "surge", "soar", "rally", "bullish", "breakout", "gain", "gains",
    "rise", "rises", "rising", "jump", "jumps", "upgrade", "partnership",
    "adoption", "record high", "all-time high", "accumulate", "inflow",
]
_BEARISH_WORDS = [
    "crash", "plunge", "plummet", "bearish", "selloff", "sell-off", "dump",
    "decline", "declines", "falls", "falling", "drop", "drops", "downgrade",
    "hack", "exploit", "lawsuit", "ban", "banned", "delist", "delisting",
    "outflow",
]


def get_headlines(asset="XLM", limit=10):
    """Fetch recent headlines for `asset`. Returns [] on any failure (network
    error, rate limit, unexpected response shape) so callers can treat an
    empty list as "no signal" rather than needing their own error handling.
    """
    tag = _ASSET_TAGS.get(asset.upper(), asset.lower())
    url = _RSS_URL_TEMPLATE.format(tag=tag)
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall("./channel/item")[:limit]
        return [
            {
                "title": (item.findtext("title") or "").strip(),
                "source": "cointelegraph",
                "published_at": item.findtext("pubDate"),
                "url": (item.findtext("link") or "").strip(),
            }
            for item in items
        ]
    except Exception as e:
        print(f"[news_feed] error fetching headlines: {e}")
        return []


def sentiment_score(headlines=None, asset="XLM"):
    """Rough bullish/bearish score in [-1, 1] from keyword counts in headline
    titles. 0.0 if there are no headlines or no keyword hits at all.
    """
    if headlines is None:
        headlines = get_headlines(asset)
    if not headlines:
        return 0.0

    bull_hits = 0
    bear_hits = 0
    for h in headlines:
        title = h.get("title", "").lower()
        bull_hits += sum(1 for w in _BULLISH_WORDS if w in title)
        bear_hits += sum(1 for w in _BEARISH_WORDS if w in title)

    total = bull_hits + bear_hits
    if total == 0:
        return 0.0
    return (bull_hits - bear_hits) / total


if __name__ == "__main__":
    heads = get_headlines()
    print(f"{len(heads)} headlines, sentiment={sentiment_score(heads):.2f}")
    for h in heads:
        print(f"  - {h['title']}")

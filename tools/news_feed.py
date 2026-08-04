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

Results are cached in-process (see _CACHE_TTL_S). Callers are trading loops
ticking every 30s and, worse, backtest replays that call decide() ~86,000
times for a 30-day run -- uncached, either one would hammer Cointelegraph
into a rate-limit ban, and the backtest would take hours instead of a second.
Headlines do not move on a 30-second scale, so a stale-by-minutes answer is
the same answer. Strategies should still not call this from decide(): fetch
once per tick in the trading loop and pass the score in via state, so the
decide step stays pure, fast and replayable. See template_repo/main.py.
"""
import re
import time
import xml.etree.ElementTree as ET

import requests

_TIMEOUT = 10
_RSS_URL_TEMPLATE = "https://cointelegraph.com/rss/tag/{tag}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; xlm-trading-bot/1.0)"}
# Cointelegraph's per-tag RSS slugs aren't always the ticker itself.
_ASSET_TAGS = {"XLM": "stellar", "STELLAR": "stellar"}

# How long a successful fetch is reused, and how long a *failure* is reused.
# The failure TTL is short (the feed should be retried soon) but non-zero and
# deliberately not skipped: without it, a down feed costs a full _TIMEOUT of
# dead wait on every 30s tick, i.e. a third of the tick budget spent blocking.
_CACHE_TTL_S = 900
_ERROR_TTL_S = 60
# (tag, limit) -> (fetched_at, headlines)
_cache = {}

_BULLISH_WORDS = [
    "surge", "surges", "surged", "soar", "soars", "soared", "rally",
    "rallies", "rallied", "bullish", "breakout", "gain", "gains", "gained",
    "rise", "rises", "rising", "jump", "jumps", "jumped", "upgrade",
    "upgrades", "partnership", "partnerships", "adoption", "record high",
    "all-time high", "accumulate", "inflow", "inflows",
]
_BEARISH_WORDS = [
    "crash", "crashes", "plunge", "plunges", "plummet", "bearish", "selloff",
    "sell-off", "dump", "decline", "declines", "falls", "falling", "drop",
    "drops", "downgrade", "hack", "hacked", "hackers", "exploit", "exploits",
    "lawsuit", "lawsuits", "ban", "bans", "banned", "delist", "delisting",
    "outflow", "outflows",
]

# Keywords are matched on WORD BOUNDARIES, not as substrings. The obvious
# `word in title` scored "US Bancorp launches stablecoin pilot" as bearish --
# "ban" is a substring of "Bancorp" -- and with only ~10 headlines a single
# false hit and no bullish hits pins the score at -1.0, the most bearish value
# there is. Anything keying a trade rule off that (see news_veto_below in
# template_repo/config.json) would have been stuck permanently on.
# The cost of \b is that inflections no longer match for free, so the lists
# above spell them out rather than reverting to substrings: `\bban\w*` would
# match "Bancorp" all over again.
_WORD_RES = {}

# Below this many total keyword hits the score is noise, not signal, and is
# reported as 0.0. This is a ratio over a ~10-headline sample: one lone hit
# yields +/-1.0, which reads as "maximally bullish/bearish" when it actually
# means "one headline contained one word".
_MIN_HITS = 3


def _hits(words, title):
    """How many of `words` appear in `title` as whole words."""
    count = 0
    for w in words:
        pattern = _WORD_RES.get(w)
        if pattern is None:
            pattern = _WORD_RES[w] = re.compile(r"\b" + re.escape(w) + r"\b")
        if pattern.search(title):
            count += 1
    return count


def get_headlines(asset="XLM", limit=10, max_age=None):
    """Fetch recent headlines for `asset`. Returns [] on any failure (network
    error, rate limit, unexpected response shape) so callers can treat an
    empty list as "no signal" rather than needing their own error handling.

    Cached for `max_age` seconds (default _CACHE_TTL_S); pass max_age=0 to
    force a live fetch. A cached failure is an empty list and expires after
    _ERROR_TTL_S instead.
    """
    tag = _ASSET_TAGS.get(asset.upper(), asset.lower())
    url = _RSS_URL_TEMPLATE.format(tag=tag)

    key = (tag, limit)
    now = time.time()
    cached = _cache.get(key)
    if cached is not None:
        fetched_at, headlines = cached
        ttl = _CACHE_TTL_S if headlines else _ERROR_TTL_S
        if max_age is not None:
            ttl = max_age
        if now - fetched_at < ttl:
            return list(headlines)

    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall("./channel/item")[:limit]
        headlines = [
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
        headlines = []

    _cache[key] = (now, headlines)
    return list(headlines)


def sentiment_score(headlines=None, asset="XLM"):
    """Rough bullish/bearish score in [-1, 1] from keyword counts in headline
    titles. Returns 0.0 (neutral) when there are no headlines, or when there
    are fewer than _MIN_HITS keyword hits to base a ratio on.
    """
    if headlines is None:
        headlines = get_headlines(asset)
    if not headlines:
        return 0.0

    bull_hits = 0
    bear_hits = 0
    for h in headlines:
        title = h.get("title", "").lower()
        bull_hits += _hits(_BULLISH_WORDS, title)
        bear_hits += _hits(_BEARISH_WORDS, title)

    total = bull_hits + bear_hits
    if total < _MIN_HITS:
        return 0.0
    return (bull_hits - bear_hits) / total


if __name__ == "__main__":
    heads = get_headlines()
    print(f"{len(heads)} headlines, sentiment={sentiment_score(heads):.2f}")
    for h in heads:
        print(f"  - {h['title']}")

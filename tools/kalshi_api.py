#!/usr/bin/env python3
"""Keyless reads from Kalshi's public REST API -- the "world" for domain_kalshi.py.

Kalshi exposes free, keyless REST reads for markets, series, orderbooks and trades (see
KALSHI.md's Phase 0 spike, confirmed live 2026-08-10/11). This module is the thin,
dependency-free wrapper dex_price.py/price_feed.py already establish the pattern for in
this codebase: every function returns None (or an empty list) on any failure rather than
raising, and nothing here requires an account or an API key -- Phase 2's signed-request
trading path is a separate module (tools/kalshi_trader.py, not built yet).

FIELD NAMES, confirmed live against external-api.kalshi.com (docs.kalshi.com's
documented base; api.elections.kalshi.com answers identically and works as a fallback):
`last_price_dollars`, `yes_bid_dollars`/`yes_ask_dollars`, `volume_fp`,
`open_interest_fp`, `status` ('active' while trading, 'finalized' once settled), `result`
('yes'/'no'/'' unresolved), `close_time` (RFC3339, UTC, 'Z' suffix).

WHY implied_prob = last_price, NOT a bid/ask midpoint
=======================================================
KALSHI.md's Phase 0 answer #1: the order book is often one-sided (confirmed live on
KXHIGHCHI-26AUG10-B86.5: empty yes side, only no side populated), so a midpoint isn't
always computable. last_price_dollars is always populated (0.0000 for an unpriced
market, which is itself a signal -- "nothing has priced this yet"). Filtering out
markets nobody has traded is the CALLER's job (a minimum volume/open_interest floor),
not this module's -- kalshi_recorder.py and domain_kalshi.py's config knobs are where
that floor gets enforced, the same separation dex_price.py draws between get_mark() (a
raw read) and mark_value() (the gamed-position-resistant one).

CATEGORY FILTERING -- /series only, not /markets or /events
==============================================================
Confirmed live 2026-08-11: `GET /markets?category=X` and `GET /events?category=X`
silently ignore the category param and return unfiltered results (a "Climate and
Weather" filter on /markets returned World/Elections/Sports markets untouched). Only
`GET /series?category=X` actually filters. So category selection here means: resolve a
category to its series tickers via list_series() (cached -- series move slowly), then
pull open markets per series_ticker. There is no single "every open market in category
X" call.

KNOWN_CATEGORIES was built by crawling every series with no category filter and taking
the distinct `category` values (18 of them, 2026-08-11) -- a real, live-verified list,
not a guess, kept here so domain_kalshi.py's config_is_sane can validate a category_filter
offline without a network round trip on every check.

Nothing here talks to /portfolio, /orders, or anything requiring
KALSHI-ACCESS-KEY/-SIGNATURE headers -- that is Phase 2, a signed, KYC'd account, a
different module entirely.
"""
import datetime
import json
import time
from pathlib import Path

import requests

_BASE = 'https://external-api.kalshi.com/trade-api/v2'
_TIMEOUT = 10

# Shared on-disk cache, same shape as dex_price.py's -- a strategy population's tick
# loop must not each fetch their own copy of the same market.
_CACHE_PATH = Path(__file__).resolve().parent / '.kalshi_cache.json'
_MARKET_CACHE_TTL = 30
_SERIES_CACHE_TTL = 6 * 3600   # series come and go on the order of days, not minutes

# Confirmed live 2026-08-11 by crawling every series with no category filter and taking
# the distinct category values. See module docstring.
KNOWN_CATEGORIES = (
    'Climate and Weather', 'Commodities', 'Companies', 'Crypto', 'Economics',
    'Education', 'Elections', 'Entertainment', 'Exotics', 'Financials', 'Health',
    'Mentions', 'Politics', 'Science and Technology', 'Social', 'Sports',
    'Transportation', 'World',
)


def _cache_read(key, ttl):
    try:
        data = json.loads(_CACHE_PATH.read_text())
        entry = data.get(key)
        if entry and time.time() - entry['ts'] < ttl:
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
        # Drop entries far past their TTL so the file can't grow without bound.
        cutoff = time.time() - _SERIES_CACHE_TTL * 4
        data = {k: v for k, v in data.items() if v.get('ts', 0) > cutoff}
        tmp = _CACHE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(data))
        tmp.replace(_CACHE_PATH)   # atomic; strategies read this concurrently
    except Exception:
        pass


# KALSHI.md's Phase 0 spike found no enforced ceiling at ~12 req/s (a 25-request burst
# all returned 200). Building a fixed backtest set (kalshi_backtest.py) issues far more
# requests than that in a tight loop and DOES draw 429s in practice (observed live
# 2026-08-11) -- so this module rate-limits and retries itself rather than assume the
# earlier, smaller-scale measurement covers every caller. A shared module-level
# timestamp is enough: every caller in this process, including a population of
# strategies each importing this module, goes through the same _get().
_MIN_INTERVAL = 0.15     # ~6-7 req/s steady state, comfortably under the observed ceiling
_MAX_RETRIES = 3
_last_request_ts = [0.0]


def _get(path, params=None):
    for attempt in range(_MAX_RETRIES + 1):
        wait = _MIN_INTERVAL - (time.time() - _last_request_ts[0])
        if wait > 0:
            time.sleep(wait)
        _last_request_ts[0] = time.time()
        resp = requests.get(f'{_BASE}{path}', params=params or {}, timeout=_TIMEOUT)
        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            time.sleep(2 ** attempt)   # 1s, 2s, 4s
            continue
        resp.raise_for_status()
        return resp.json()


def _dollars(value):
    """'0.0100' -> 0.01. None on anything unparseable, never raises."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch(iso_ts):
    """RFC3339 'YYYY-MM-DDTHH:MM:SSZ' (with or without fractional seconds) -> unix
    epoch seconds, or None."""
    if not iso_ts:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S.%fZ'):
        try:
            return datetime.datetime.strptime(iso_ts, fmt).replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except Exception:
            continue
    return None


def _normalize_market(raw):
    """Kalshi's market object -> the flat shape every caller in this domain reads."""
    return {
        'ticker': raw.get('ticker'),
        'event_ticker': raw.get('event_ticker'),
        'title': raw.get('title'),
        'status': raw.get('status'),
        'result': raw.get('result') or '',
        'implied_prob': _dollars(raw.get('last_price_dollars')),
        'yes_bid': _dollars(raw.get('yes_bid_dollars')),
        'yes_ask': _dollars(raw.get('yes_ask_dollars')),
        'volume': _dollars(raw.get('volume_fp')),
        'open_interest': _dollars(raw.get('open_interest_fp')),
        'open_time': _epoch(raw.get('open_time')),
        'close_time': _epoch(raw.get('close_time')),
    }


def get_market(ticker):
    """One market's current state, or None on any failure."""
    if not ticker:
        return None
    cached = _cache_read(f'market:{ticker}', _MARKET_CACHE_TTL)
    if cached is not None:
        return cached
    try:
        raw = _get(f'/markets/{ticker}').get('market')
    except Exception as e:
        print(f'[kalshi_api] get_market {ticker}: {e}')
        return None
    if not raw:
        return None
    market = _normalize_market(raw)
    _cache_write(f'market:{ticker}', market)
    return market


def get_resolved(ticker):
    """{'ticker', 'result': 'yes'/'no', 'close_time'} once Kalshi reports a settlement,
    else None -- "not yet settled" and "could not be fetched" collapse to the same
    answer, same as dex_price's absence policy; the caller (kalshi_reconcile.py) just
    tries again later."""
    market = get_market(ticker)
    if market is None or market.get('status') != 'finalized':
        return None
    if market.get('result') not in ('yes', 'no'):
        return None
    return {'ticker': ticker, 'result': market['result'], 'close_time': market.get('close_time')}


def ping():
    """True if a live, uncached request to the API succeeds -- the reachability half of
    domain_kalshi.observe().

    Deliberately NOT routed through list_series(): that answer is cached for
    _SERIES_CACHE_TTL (6h), so it cannot tell a reachable API from a stale copy of one,
    which is precisely the distinction a reachability probe exists to make. limit=1 keeps
    it to a single cheap request through the same rate limiter every other caller uses.
    """
    try:
        _get('/series', {'limit': 1})
        return True
    except Exception as e:
        print(f'[kalshi_api] ping: {e}')
        return False


def list_series(category=None, frequency=None, max_pages=20):
    """[{ticker, title, category, frequency}, ...] -- the only call that actually
    filters by category (see module docstring). Cached hours at a time."""
    cache_key = f'series:{category or "*"}'
    cached = _cache_read(cache_key, _SERIES_CACHE_TTL)
    if cached is None:
        out = []
        cursor = None
        for _ in range(max_pages):
            params = {'limit': 200}
            if category:
                params['category'] = category
            if cursor:
                params['cursor'] = cursor
            try:
                data = _get('/series', params)
            except Exception as e:
                print(f'[kalshi_api] list_series({category!r}): {e}')
                break
            for s in data.get('series') or []:
                out.append({'ticker': s.get('ticker'), 'title': s.get('title'),
                            'category': s.get('category'), 'frequency': s.get('frequency')})
            cursor = data.get('cursor')
            if not cursor:
                break
        _cache_write(cache_key, out)
        cached = out
    if frequency:
        cached = [s for s in cached if s.get('frequency') == frequency]
    return cached


def list_markets(series_ticker, status='open', limit=100):
    """Markets for one `series_ticker` in a given query `status`, normalized. [] on
    failure. `status` is a QUERY filter, not the field name the market object itself
    reports: confirmed live 2026-08-11 that status='open' returns objects with
    status='active', and status='settled' returns objects with status='finalized' --
    this function hides that mismatch so callers only ever deal with the returned
    field's own vocabulary (see _normalize_market)."""
    if not series_ticker:
        return []
    try:
        data = _get('/markets', {'series_ticker': series_ticker, 'status': status,
                                  'limit': limit})
    except Exception as e:
        print(f'[kalshi_api] list_markets series={series_ticker} status={status}: {e}')
        return []
    out = []
    for raw in data.get('markets') or []:
        market = _normalize_market(raw)
        market['series_ticker'] = series_ticker   # not on the raw object; we already know it
        out.append(market)
    return out


def list_open_markets(category=None, series_ticker=None, frequency=None,
                       max_series=25, limit_per_series=100):
    """Open markets for one `series_ticker`, or across every series in `category`
    (optionally narrowed to `frequency`, e.g. 'daily' -- see KALSHI.md's Phase 0 answer
    #2 recommending Climate and Weather dailies as the Phase 1 default).

    Queries one series at a time -- category alone is not filterable server-side, see
    the module docstring -- and caps how many series a single call touches
    (`max_series`), so a category with dozens of series doesn't turn one poll into
    dozens of sequential requests. kalshi_recorder.py round-robins across series between
    polls rather than asking this function to cover a whole category at once.
    """
    if series_ticker:
        tickers = [series_ticker]
    else:
        series = list_series(category=category, frequency=frequency)
        tickers = [s['ticker'] for s in series[:max_series] if s.get('ticker')]
    out = []
    for t in tickers:
        out.extend(list_markets(t, status='open', limit=limit_per_series))
    return out


def list_resolved_markets(category=None, series_ticker=None, frequency=None,
                           max_series=25, limit_per_series=100):
    """Settled markets (status='finalized', a real 'yes'/'no' result) -- the fixed
    replay set kalshi_backtest.py builds itself from. Same one-series-at-a-time shape as
    list_open_markets; see that function's docstring for why."""
    if series_ticker:
        tickers = [series_ticker]
    else:
        series = list_series(category=category, frequency=frequency)
        tickers = [s['ticker'] for s in series[:max_series] if s.get('ticker')]
    out = []
    for t in tickers:
        for m in list_markets(t, status='settled', limit=limit_per_series):
            if m.get('result') in ('yes', 'no'):
                out.append(m)
    return out


def get_candlesticks(series_ticker, ticker, start_ts, end_ts, period_interval=60):
    """[{'ts', 'close', 'volume'}, ...] oldest first, over [start_ts, end_ts] (unix
    seconds) -- confirmed live 2026-08-11 this endpoint 400s without both bounds.
    `close` is the yes-side last price of that candle, in the same [0, 1] units as
    implied_prob elsewhere in this module. [] on any failure -- used by
    kalshi_backtest.py to read a resolved market's price at a point BEFORE it
    converged toward the answer, not to serve a live tick loop."""
    try:
        data = _get(f'/series/{series_ticker}/markets/{ticker}/candlesticks',
                     {'start_ts': int(start_ts), 'end_ts': int(end_ts),
                      'period_interval': period_interval})
    except Exception as e:
        print(f'[kalshi_api] get_candlesticks {ticker}: {e}')
        return []
    out = []
    for c in data.get('candlesticks') or []:
        close = _dollars((c.get('price') or {}).get('close_dollars'))
        if close is None:
            continue
        out.append({'ts': c.get('end_period_ts'), 'close': close,
                    'volume': _dollars(c.get('volume_fp'))})
    return out


if __name__ == '__main__':
    import sys
    cat = sys.argv[1] if len(sys.argv) > 1 else 'Climate and Weather'
    series = list_series(category=cat, frequency='daily')
    print(f'{len(series)} daily series in {cat!r}')
    markets = list_open_markets(category=cat, frequency='daily', max_series=5)
    print(f'{len(markets)} open markets across the first 5 series:')
    for m in markets[:10]:
        print(f"  {m['ticker']}: implied={m['implied_prob']} vol={m['volume']} "
              f"oi={m['open_interest']} status={m['status']}")

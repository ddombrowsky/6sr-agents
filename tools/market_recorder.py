#!/usr/bin/env python3
"""A durable record of market conditions, one row per monitor cycle.

What history this system keeps today is entirely price and entirely one venue:
ohlc_history.py caches ~30 days of Kraken/Coinbase XLM candles, and
price_history_fetcher.py holds a short rolling buffer of live spot samples. Nothing at
all is retained about the venue the money actually trades on -- not the DEX book, not
its width, not its depth, not the basis against the CEX, not the news sentiment that
news_feed.py has been able to compute since the first emperor pass and which no
strategy has ever consumed.

That absence is why every post-mortem in this repo has had to re-derive conditions from
scratch, and why no strategy can condition on anything but price: you cannot regress
against data you never wrote down. One row an hour is ~9 KB a day and makes questions
like "does our edge survive when the book is wide?" or "is the basis mean-reverting on
an hourly scale?" answerable at all.

Deliberately append-only JSONL, not a database: monitor writes it, strategies and the
next emperor pass read it, and a partially-written last line costs one row rather than
the file. Everything here degrades to None rather than raising -- this is called from
monitor's cycle and must never be able to cost one.
"""
import json
import os
import time
from pathlib import Path

HISTORY_PATH = Path('/opt/trades/.market_history.jsonl')

# ~1 row/hour, so this is a bit over a year. Trimming happens on write and only when the
# file is over the cap, which at one row an hour means approximately never.
MAX_ROWS = 10000


def _safe(fn, *args, **kwargs):
    """Call fn, returning None on absolutely anything. Used per-field, deliberately:
    a Horizon timeout should cost that one column, not the whole snapshot."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def snapshot(spec='XLM'):
    """Current market conditions as a plain dict. No I/O to disk.

    Every field is independently optional. A row with a null sentiment and a real book
    is still worth keeping; the point is the time series, not any single reading.
    """
    row = {'ts': time.time(), 'spec': spec}

    try:
        import dex_price
        import friction
        from price_feed import get_price
    except Exception:
        return row

    row['cex_mid'] = _safe(get_price) if spec == 'XLM' else _safe(dex_price.get_mark, spec)

    book = _safe(dex_price.get_orderbook, spec) or {}
    row['dex_bid'] = book.get('best_bid')
    row['dex_ask'] = book.get('best_ask')
    row['dex_mid'] = book.get('mid')
    row['spread_bp'] = round(book['spread_pct'] * 10000, 2) if book.get('spread_pct') else None
    row['bid_depth_usd'] = book.get('bid_depth_usd')
    row['ask_depth_usd'] = book.get('ask_depth_usd')

    half = _safe(friction.half_spread, spec)
    row['half_spread_bp'] = round(half * 10000, 2) if half else None

    if row.get('cex_mid') and row.get('dex_mid'):
        row['basis_bp'] = round((row['dex_mid'] - row['cex_mid']) / row['cex_mid'] * 10000, 2)
        if row.get('half_spread_bp') is not None:
            row['tradeable_bp'] = round(abs(row['basis_bp']) - row['half_spread_bp'], 2)

    # Coarse and keyword-based, but genuinely orthogonal to price -- and free, since
    # news_feed already caches its fetch. Recorded even though nothing reads it yet:
    # that is exactly the field this module exists to stop losing.
    try:
        from news_feed import sentiment_score
        row['sentiment'] = _safe(sentiment_score)
    except Exception:
        row['sentiment'] = None

    return row


def record_snapshot(spec='XLM'):
    """Append one snapshot row. Returns the row, or None if nothing could be written."""
    row = snapshot(spec)
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open('a') as f:
            f.write(json.dumps(row) + '\n')
        _trim()
        return row
    except Exception:
        return None


def _trim():
    """Keep the file under MAX_ROWS. Rewrites via a temp file so a reader mid-trim
    sees either the old file or the new one, never a truncated one."""
    try:
        if not HISTORY_PATH.exists():
            return
        # Cheap guard: only pay for reading the file when it is plausibly too long.
        if os.path.getsize(HISTORY_PATH) < MAX_ROWS * 200:
            return
        lines = HISTORY_PATH.read_text().splitlines()
        if len(lines) <= MAX_ROWS:
            return
        tmp = HISTORY_PATH.with_suffix('.tmp')
        tmp.write_text('\n'.join(lines[-MAX_ROWS:]) + '\n')
        tmp.replace(HISTORY_PATH)
    except Exception:
        pass


def read_history(hours=168, spec=None):
    """Recorded rows from the last `hours`, oldest first. [] if there are none.

    Malformed lines are skipped rather than raising: this file is appended to from a
    long-running process, so a torn final line is a normal state, not corruption.
    """
    if not HISTORY_PATH.exists():
        return []
    cutoff = time.time() - float(hours) * 3600
    out = []
    try:
        with HISTORY_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get('ts', 0) < cutoff:
                    continue
                if spec and row.get('spec') != spec:
                    continue
                out.append(row)
    except Exception:
        return out
    return out


def series(field, hours=168, spec=None):
    """One column as a list, oldest first, skipping rows where it is missing.

    The shape indicators want: `series('basis_bp', 72)` feeds straight into
    moving_averages.exponential_moving_average or rsi.rsi.
    """
    return [row[field] for row in read_history(hours, spec)
            if row.get(field) is not None]


def summary(hours=24, spec='XLM'):
    """Min/mean/max of the interesting columns over `hours`, for a log line."""
    rows = read_history(hours, spec)
    if not rows:
        return None
    out = {'rows': len(rows), 'hours': hours}
    for field in ('spread_bp', 'basis_bp', 'tradeable_bp', 'sentiment'):
        values = [r[field] for r in rows if r.get(field) is not None]
        if values:
            out[field] = {'min': min(values),
                          'mean': round(sum(values) / len(values), 3),
                          'max': max(values)}
    return out


if __name__ == '__main__':
    import sys
    if '--record' in sys.argv:
        print(json.dumps(record_snapshot(), indent=2))
    else:
        print(json.dumps(snapshot(), indent=2))
        s = summary()
        print('\nlast 24h:', json.dumps(s, indent=2) if s else 'no history recorded yet')

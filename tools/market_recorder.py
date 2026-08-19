#!/usr/bin/env python3
"""A durable record of market conditions, one row a minute.

This is now the shared transport for the DEX/CEX basis, and the cadence is the whole
reason. It was one row per monitor cycle (~1h) when it only had to answer post-mortem
questions. A basis is a per-tick input: a strategy on a 30s loop, and backtest.py on a
1-minute candle grid, both need a reading no older than a few minutes, and neither may
fetch its own -- dex_price.get_orderbook is uncached, so ~10 running strategies calling
basis.get_basis() on every tick is up to 40 Horizon order-book GETs a minute, which is a
rate-limit incident rather than a signal.

So: ONE writer (the daemon below, supervised by monitor._ensure_recorder), MANY readers
(basis.latest() tails this file, backtest._basis_series joins it to candles,
basis_report joins it to trade logs). That single-writer property is the safety
property; do not add a live-network fallback to any of the readers.

What history this system keeps today is entirely price and entirely one venue:
ohlc_history.py caches ~30 days of Kraken/Coinbase XLM candles, and
price_history_fetcher.py holds a short rolling buffer of live spot samples. Nothing at
all is retained about the venue the money actually trades on -- not the DEX book, not
its width, not its depth, not the basis against the CEX, not the news sentiment that
news_feed.py has been able to compute since the first emperor pass and which no
strategy has ever consumed.

That absence is why every post-mortem in this repo has had to re-derive conditions from
scratch, and why no strategy can condition on anything but price: you cannot regress
against data you never wrote down. One row a minute is ~430 KB a day and makes questions
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

# ~1 row/minute, so this is about 41 days. Trimming happens on write and only when the
# file is plausibly over the cap.
MAX_ROWS = 60000

# Deliberately an OVER-estimate of a row, because it is only ever used as the cheap size
# guard in _trim(). Under-estimate it and the guard trips while the file is still short
# of MAX_ROWS lines, at which point _trim reads and rewrites the entire file on EVERY
# append, forever, and never gets under the guard because there is nothing to remove.
# The old constant was 200, which was harmless at one row an hour and would have become a
# multi-megabyte rewrite per minute here.
#
# Rows measured ~300 B before the ladder below and ~1,050 B with it, so this went
# 400 -> 1600 in the same commit the ladder landed. Raising it is safe (the guard just
# trips later); lowering it below the true row size is the failure described above.
_ROW_BYTES = 1600

# How many raw book levels per side to keep. The maker fill model needs depth AT a price,
# not aggregate depth across the whole book: an offer resting at P fills only after the
# size already queued at or better than P is consumed, and bid_depth_usd/ask_depth_usd sum
# the whole 50-level book and so cannot answer that.
#
# Five, and not more, because the raw levels are here for the STRUCTURE of the touch --
# which on this pair is routinely a couple of half-cent dust orders resting a fraction of
# a basis point ahead of the real depth, and a maker's whole decision is whether to step
# in front of them. Depth further out is covered exactly, and far more compactly, by the
# cumulative curve below: measured on the live book, five levels reach only ~3 bp, while
# the depth that matters for a quote 5-40 bp out runs to $8k and beyond.
_LADDER_LEVELS = 5

# Basis points from the touch at which cumulative depth is recorded. Chosen dense near the
# touch, where a quote's queue position is decided, and sparse past 20 bp, where this book
# is thousands of dollars deep and the exact number stops changing any decision.
#
# This is the field the fill model actually reads: cum[d] is "everything resting at or
# better than d bp from the touch", which is exactly the FIFO queue ahead of a quote
# placed there -- same-price orders included, since we joined the back of that queue. Two
# arrays of 17 numbers cost ~400 B and answer at any distance; getting the same coverage
# from raw levels would take 50 of them.
_CUM_BP = (0.5, 1, 1.5, 2, 3, 4, 5, 7, 10, 15, 20, 30, 50, 75, 100, 150, 200)

RECORD_INTERVAL = 60        # what the daemon uses, and what basis.latest() assumes


def _safe(fn, *args, **kwargs):
    """Call fn, returning None on absolutely anything. Used per-field, deliberately:
    a Horizon timeout should cost that one column, not the whole snapshot."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _cum_depth(levels, touch, side):
    """Cumulative USD resting at or better than each _CUM_BP offset from `touch`.

    Returned as [[bp, usd], ...] so it survives a JSON round trip as a list of pairs (a
    dict would key on stringified floats). None when the book or the touch is missing --
    every field in a row is independently optional, and a reader must handle its absence
    anyway for the months of history recorded before this existed.

    "At or better than" deliberately includes orders at exactly that price: a quote
    joining a price level goes to the BACK of the queue already there, so that size is
    ahead of it. Treating it as behind is the single easiest way to make a backtest fill
    at prices it never would have.
    """
    if not levels or not touch or touch <= 0:
        return None
    out = []
    try:
        for bp in _CUM_BP:
            edge = touch * (1 - bp / 10000.0) if side == 'bid' else touch * (1 + bp / 10000.0)
            total = sum(lv['usd'] for lv in levels
                        if (lv['price'] >= edge if side == 'bid' else lv['price'] <= edge))
            out.append([bp, round(total, 2)])
    except Exception:
        return None
    return out


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
    # `is not None`, not a truth test: a LOCKED book (bid == ask) has spread_pct 0.0,
    # and recording that real zero as None is what makes a reader's
    # `float(row.get('spread_bp', 0.0))` raise instead of taking its default.
    row['spread_bp'] = (round(book['spread_pct'] * 10000, 2)
                        if book.get('spread_pct') is not None else None)
    row['bid_depth_usd'] = book.get('bid_depth_usd')
    row['ask_depth_usd'] = book.get('ask_depth_usd')

    # The near ladder, kept because the aggregates above cannot answer "how much is queued
    # ahead of a quote at price P" -- see _LADDER_LEVELS. Additive: every existing reader
    # (basis.latest, backtest._basis_series, basis_report) reads named keys and ignores
    # unknown ones, so old rows without these keys stay readable and new rows stay
    # readable by old code. Prices are NOT rounded -- a book 10 bp wide has meaning in the
    # 6th decimal of an XLM price. Sizes are, but to 4 dp rather than 2: the top of this
    # book is routinely half-cent dust orders resting inside the real depth, and rounding
    # those to $0.00 would report zero size queued ahead of a quote placed there, which is
    # an error in the optimistic direction on the one number the fill model is built on.
    row['bids'] = [{'p': lv['price'], 'usd': round(lv['usd'], 4)}
                   for lv in (book.get('bids') or [])[:_LADDER_LEVELS]]
    row['asks'] = [{'p': lv['price'], 'usd': round(lv['usd'], 4)}
                   for lv in (book.get('asks') or [])[:_LADDER_LEVELS]]
    row['bid_cum'] = _cum_depth(book.get('bids'), row.get('dex_bid'), 'bid')
    row['ask_cum'] = _cum_depth(book.get('asks'), row.get('dex_ask'), 'ask')

    # The book we just fetched, not another one. get_orderbook is uncached, so letting
    # half_spread fetch its own doubled the Horizon load per row and let spread_bp and
    # half_spread_bp -- which are the same measurement -- come from books up to
    # friction._CACHE_TTL apart, making tradeable_bp an incoherent mix.
    half = _safe(friction.half_spread, spec, book) if book else _safe(friction.half_spread, spec)
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
        # See _ROW_BYTES on why this estimate must sit above the true row size.
        if os.path.getsize(HISTORY_PATH) < MAX_ROWS * _ROW_BYTES:
            return
        lines = HISTORY_PATH.read_text().splitlines()
        if len(lines) <= MAX_ROWS:
            return
        tmp = HISTORY_PATH.with_suffix('.tmp')
        tmp.write_text('\n'.join(lines[-MAX_ROWS:]) + '\n')
        tmp.replace(HISTORY_PATH)
    except Exception:
        pass


def tail(n=1, max_bytes=65536):
    """The last `n` rows, oldest first, without reading the whole file.

    THE ONLY function in this module a 30s trading loop may call. read_history() scans
    every line of a file that now grows by 1440 rows a day; on a tick loop that is a
    linear scan per tick per strategy. This seeks from EOF instead, so cost is constant
    in the file's length.

    The first fragment of the read window is dropped unless the window covers the whole
    file: seeking to an arbitrary byte offset lands mid-line, and that partial line is
    not a torn write to be tolerated but a slice artifact to be discarded. Malformed
    lines are skipped for the reason read_history skips them -- a daemon appends here,
    so a torn final line is a normal state.
    """
    try:
        if not HISTORY_PATH.exists():
            return []
        size = os.path.getsize(HISTORY_PATH)
        want = max(1024, min(int(max_bytes), max(1, int(n)) * _ROW_BYTES * 4))
        start = max(0, size - want)
        with HISTORY_PATH.open('rb') as f:
            f.seek(start)
            chunk = f.read()
        lines = chunk.decode('utf-8', 'replace').splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out[-max(1, int(n)):]
    except Exception:
        return []


def span():
    """{'rows', 'first_ts', 'last_ts', 'hours'} over the whole file.

    How much history exists, which is what monitor logs each cycle and what
    monitor._inject_basis_gate gates on: an arm that conditions on the basis must not be
    created before there are enough rows to derive its threshold from.
    """
    out = {'rows': 0, 'first_ts': None, 'last_ts': None, 'hours': 0.0}
    try:
        if not HISTORY_PATH.exists():
            return out
        with HISTORY_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = json.loads(line).get('ts')
                except Exception:
                    continue
                if not ts:
                    continue
                out['rows'] += 1
                if out['first_ts'] is None:
                    out['first_ts'] = ts
                out['last_ts'] = ts
    except Exception:
        pass
    if out['first_ts'] and out['last_ts']:
        out['hours'] = round((out['last_ts'] - out['first_ts']) / 3600.0, 2)
    return out


def daemon(interval=RECORD_INTERVAL, spec='XLM'):
    """Append one row every `interval` seconds, forever. The single writer.

    Sleeps to the next wall-clock interval boundary rather than for a flat `interval`,
    so timestamps land on tidy multiples of a minute. That is not cosmetic: backtest
    joins these rows to candle close times, and drift-free row timestamps keep the join
    from straddling a bucket boundary differently on every candle.

    Every iteration is individually guarded. This process is supervised by monitor but
    outlives a single monitor cycle, so one bad snapshot must cost one row.
    """
    while True:
        try:
            row = record_snapshot(spec)
            if row:
                print(f"{row.get('ts')} basis {row.get('basis_bp')} bp, "
                      f"tradeable {row.get('tradeable_bp')} bp, "
                      f"spread {row.get('spread_bp')} bp", flush=True)
        except Exception as e:
            print(f'snapshot failed ({e})', flush=True)
        try:
            now = time.time()
            time.sleep(max(1.0, interval - (now % interval)))
        except Exception:
            time.sleep(interval)


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
    if '--daemon' in sys.argv:
        every = RECORD_INTERVAL
        if '--interval' in sys.argv:
            try:
                every = int(sys.argv[sys.argv.index('--interval') + 1])
            except Exception:
                pass
        print(f'recording {HISTORY_PATH} every {every}s', flush=True)
        daemon(every)
    elif '--record' in sys.argv:
        print(json.dumps(record_snapshot(), indent=2))
    else:
        print(json.dumps(snapshot(), indent=2))
        print('\nspan:', json.dumps(span(), indent=2))
        s = summary()
        print('\nlast 24h:', json.dumps(s, indent=2) if s else 'no history recorded yet')

#!/usr/bin/env python3
"""The executed-trade tape for a DEX pair, cached on disk and backfillable.

WHY THIS EXISTS. Every trade this system has ever made is a taker: a
`path-payment-strict-send` that crosses the spread. A resting offer earns that spread
instead of paying it, but a resting offer only earns anything when somebody crosses it --
and nothing anywhere in tools/ has ever looked at who crossed what. market_recorder.py
writes the *book* once a minute; this writes the *tape*. You cannot model a fill from the
book alone: the book says how much is queued, the tape says how much got consumed, and a
fill model needs both.

The single most important field here is the aggressor direction, and it is not a field
Horizon gives you. See `_taker_side` -- it is derived from which side of the trade was a
resting offer, which is derived from Stellar's synthetic-offer-id convention. Get it
backwards and every adverse-selection number computed downstream flips sign, which is the
kind of bug that looks like a profitable strategy.

READ-ONLY. Nothing here places, cancels or prices an order, and it deliberately does not
import stellar_trader: the issuer address comes from assets.py, which is the tools-layer
canonical source for it, so this module stays outside the money boundary and outside
check_boundary_integrity's watch.

Shape is modelled on ohlc_history.py -- keyless HTTP, disk cache, degrade to empty rather
than raise -- with one difference that matters: the cache is append-only JSONL rather than
a rewritten JSON blob, because it grows to hundreds of thousands of rows and a rewrite per
fetch would be the failure market_recorder._ROW_BYTES documents.

Typical use:

    import sys; sys.path.append('/opt/tools')
    import dex_trades
    dex_trades.backfill(days=7)                  # once, ~10 min, resumable
    dex_trades.sync()                            # cheap, catches up to now
    trades = dex_trades.get_trades(start_ts=..., end_ts=...)
    print(dex_trades.tape_stats(hours=24))
"""
import calendar
import json
import time
from pathlib import Path

import requests

import assets

_HORIZON = 'https://horizon.stellar.org'
_TIMEOUT = 20
_PAGE_LIMIT = 200               # Horizon's maximum for /trades

CACHE_DIR = Path('/opt/trades')

# Stellar marks the aggressing side of a trade with a synthetic offer id at or above
# 2^62 rather than a real one: an offer that rested in the book has an id assigned by
# sequence, an offer that crossed on creation (or a path payment, which has no offer at
# all) gets one from this range. Kept for diagnostics only -- it says WHICH SIDE aggressed
# and not in which DIRECTION, and mistaking the one for the other is the bug documented at
# length in _taker_side.
_SYNTHETIC_OFFER_ID = 1 << 62

# Below this, a trade is dust that could not have moved a quote we would actually place.
# Applied at READ time, never at write time: it is ~85% of the row count and ~0.1% of the
# volume, and the day someone wants to know whether the dust is a signal, the rows have to
# already be on disk -- the tape is not re-fetchable indefinitely and a filter applied on
# write is a decision that cannot be revisited.
DUST_USD = 0.01

# Between page requests. Horizon rate-limits by IP and a backfill is thousands of pages;
# this is what keeps a backfill from becoming a rate-limit incident on the same host the
# live trader shares.
_PAGE_SLEEP = 0.12


def _norm(spec):
    """An asset spec assets.py will accept, resolving the bare code 'USDC'.

    assets.parse rejects a bare credit code on purpose -- a code without an issuer is not
    an identity and accepting one is how an impersonating asset gets traded. That rule is
    right and stays; this only maps the single literal 'USDC' onto assets.usdc(), the
    canonical spec, so the signatures in this module can read as the pair a human would
    write. Any other bare code still raises.
    """
    if spec == 'USDC':
        return assets.usdc()
    return spec


def _pair_params(spec='XLM', quote='USDC'):
    """Horizon base_/counter_ parameters naming the pair.

    Base is `spec` and counter is `quote`, which fixes the meaning of `price` (counter
    units per base unit) and of `base_is_seller`. Never write an issuer address here --
    assets.py owns it, and an issuer typed from memory is an impersonation.

    The prefix is 'base_asset', not 'base' -- the same one dex_price.py:259 uses for
    /trade_aggregations. This is worth a comment because getting it wrong does not fail:
    Horizon IGNORES parameters it does not recognise, so `base_type=native` returns the
    unfiltered global tape with a 200 OK, and the first symptom is a volume figure five
    orders of magnitude too large in a stats function nobody was checking against a
    known-good number.
    """
    params = {}
    params.update(assets.horizon_params(_norm(spec), 'base_asset'))
    params.update(assets.horizon_params(_norm(quote), 'counter_asset'))
    return params


def _cache_path(spec='XLM', quote='USDC'):
    code, _ = assets.parse(_norm(spec))
    qcode, _ = assets.parse(_norm(quote))
    return CACHE_DIR / f'.dex_trades_{code}_{qcode}.jsonl'


def _meta_path(spec='XLM', quote='USDC'):
    return _cache_path(spec, quote).with_suffix('.meta.json')


def _token_key(token):
    """A paging token as a sortable tuple. Trade tokens are '<operation_id>-<order>'.

    Compared numerically, not as strings: '9-1' sorts after '10-1' lexically and before
    it numerically, and the numeric order is the one Horizon pages in.
    """
    try:
        head, _, tail = str(token).partition('-')
        return (int(head), int(tail or 0))
    except Exception:
        return (0, 0)


def _load_meta(spec, quote):
    """{'min_token', 'max_token'} -- the ONE contiguous token range already on disk.

    A single interval rather than a set of seen tokens is the whole resumability design:
    a 30-day tape is ~1.8M trades and a set of that many tokens is a per-process memory
    cost paid on every read. Because both `backfill` (older) and `sync` (newer) only ever
    extend the interval outward from a range that started contiguous, "already have it"
    is exactly "inside the interval", in O(1).
    """
    try:
        with _meta_path(spec, quote).open() as f:
            meta = json.load(f)
        if meta.get('min_token') and meta.get('max_token'):
            return meta
    except Exception:
        pass
    return None


def _save_meta(spec, quote, meta):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _meta_path(spec, quote)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(meta))
        tmp.replace(path)
    except Exception as e:
        print(f'[dex_trades] could not save meta: {e}')


def _extend_meta(spec, quote, tokens):
    if not tokens:
        return
    meta = _load_meta(spec, quote) or {}
    lo = min(tokens, key=_token_key)
    hi = max(tokens, key=_token_key)
    if not meta.get('min_token') or _token_key(lo) < _token_key(meta['min_token']):
        meta['min_token'] = lo
    if not meta.get('max_token') or _token_key(hi) > _token_key(meta['max_token']):
        meta['max_token'] = hi
    _save_meta(spec, quote, meta)


def _parse_time(s):
    """'2026-08-17T02:54:59Z' -> epoch seconds. 0 on anything unparseable.

    calendar.timegm, not time.mktime: Horizon stamps in UTC and mktime interprets its
    argument as LOCAL time. Subtracting time.timezone afterwards looks like it corrects
    for that and does not -- time.timezone is the standard-time offset while mktime
    applies the DST-aware one, so on a host in a DST zone every timestamp lands an hour
    off. That silently shifts the whole tape relative to the book snapshots it is joined
    to, which is a fill model reading the wrong minute's queue.
    """
    try:
        return calendar.timegm(time.strptime(s, '%Y-%m-%dT%H:%M:%SZ'))
    except Exception:
        return 0


def _taker_side(rec):
    """Which side of the book this trade consumed: 'buy', 'sell', or None if unknowable.

    'buy' means the aggressor BOUGHT the base asset and therefore consumed ASKS; 'sell'
    means it sold base and consumed BIDS. This is what a fill model needs: a resting bid
    of ours can only be filled by a trade whose taker was selling.

    The rule is one field:

        base_is_seller == True   ->  an ask was consumed  ->  taker_side 'buy'
        base_is_seller == False  ->  a bid was consumed   ->  taker_side 'sell'

    `base_is_seller` does NOT mean "the base account gave up base asset", which is the
    obvious reading and the wrong one. It means the RESTING side of the trade was the one
    whose offer sold the base asset -- and an offer selling XLM for USDC is an ask. That is
    why one boolean settles it and the offer ids are not needed.

    This was got backwards once, on the plausible-sounding theory that Stellar's synthetic
    offer id (>= 2^62, marking the side that aggressed) identifies the maker and
    `base_is_seller` then gives its direction. That theory labelled 98% of the tape as
    taker-buys, which is not what a two-sided market looks like. It was settled by pulling
    the aggressor's own operation off Horizon for 36 single-hop trades whose direction the
    operation states outright (path_payment_strict_send/receive with no `path`, and
    manage_sell_offer/manage_buy_offer): the rule above matched 36/36, the offer-id theory
    matched 2/36. Repeat that experiment before changing this function -- the failure mode
    is silent, and it flips the sign of every adverse-selection number downstream.

    (The two are in fact redundant: `base_is_seller` was exactly `base_offer_id < 2^62` in
    every record sampled, so the offer ids carry no direction information of their own.)
    """
    value = rec.get('base_is_seller')
    if value is None:
        return None
    return 'buy' if value else 'sell'


def _row(rec):
    """One Horizon /trades record as the compact cache row, or None to skip it.

    Stored keys are short because there are ~2,500 rows an hour and the difference
    between 110 B and 200 B a row is 100 MB over a month.

    The price is kept as the rational Horizon sent (`n`/`d`) and never as a float. A
    DEX price of ~$0.158 with a 10 bp spread differs between levels in the sixth decimal;
    dividing on the way in and multiplying on the way out is a rounding error deliberately
    injected into the one number the whole spread measurement rests on.
    """
    try:
        price = rec.get('price') or {}
        return {
            't': round(_parse_time(rec.get('ledger_close_time') or ''), 3),
            'k': rec.get('paging_token'),
            'n': int(price['n']),
            'd': int(price['d']),
            'b': float(rec['base_amount']),
            'c': float(rec['counter_amount']),
            's': _taker_side(rec),
            # 'x' marks a liquidity-pool trade. Cached rather than dropped, for the same
            # reason the dust is: a pool trade cannot fill a resting offer, so the fill
            # model must exclude it, but "what fraction of this pair's flow is
            # uncapturable by a maker?" is a sizing question and it cannot be answered
            # from a file that only kept the capturable half. Excluded by default in
            # get_trades; counted by tape_stats.
            **({'x': 'p'} if rec.get('trade_type') == 'liquidity_pool' else {}),
        }
    except Exception:
        return None


def _expand(row):
    """A cache row as the public Trade dict.

    `usd` assumes the quote asset is USDC at $1, which is the only pair this module is
    used for and is stated in get_trades' docstring rather than silently assumed here.
    """
    return {
        'ts': row['t'],
        'price': row['n'] / row['d'] if row.get('d') else None,
        'base_amount': row['b'],
        'usd': row['c'],
        'taker_side': row.get('s'),
        'paging_token': row.get('k'),
    }


def _fetch_page(spec, quote, cursor=None, order='desc'):
    """One page of /trades as raw Horizon records, newest-first for order='desc'."""
    params = {'limit': _PAGE_LIMIT, 'order': order}
    params.update(_pair_params(spec, quote))
    if cursor:
        params['cursor'] = cursor
    resp = requests.get(f'{_HORIZON}/trades', params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get('_embedded', {}).get('records') or []


def _append(spec, quote, rows):
    if not rows:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _cache_path(spec, quote).open('a') as f:
        for row in rows:
            f.write(json.dumps(row, separators=(',', ':')) + '\n')


def _page_loop(spec, quote, cursor, order, stop, label):
    """Page until `stop(row)` is true, appending everything outside the covered range.

    Shared by backfill (order='desc', walking back in time) and sync (order='asc',
    walking forward). Every network error ends the loop rather than raising: a partial
    tape is useful and a traceback out of a data-collection helper is not, and the
    covered-range meta is written as we go so the next call resumes where this stopped.
    """
    meta = _load_meta(spec, quote) or {}
    have_lo, have_hi = meta.get('min_token'), meta.get('max_token')
    written = pages = 0
    last_ts = None
    while True:
        try:
            records = _fetch_page(spec, quote, cursor, order)
        except Exception as e:
            print(f'[dex_trades] {label} stopped on {e}')
            break
        if not records:
            break
        pages += 1
        cursor = records[-1].get('paging_token')
        rows, tokens, done = [], [], False
        for rec in records:
            # Every record's token joins the covered range, including the pool trades and
            # malformed rows _row drops. The range has to be contiguous over what HORIZON
            # returned, not over what we chose to keep: skip a token here and it reads as
            # uncovered on the next call, which re-fetches the rows around it and appends
            # them a second time.
            token = rec.get('paging_token')
            if token:
                tokens.append(token)
            row = _row(rec)
            if row is None:
                continue
            last_ts = row['t']
            if stop(row):
                done = True
                break
            inside = (have_lo and have_hi
                      and _token_key(have_lo) <= _token_key(token) <= _token_key(have_hi))
            if not inside:
                rows.append(row)
        _append(spec, quote, rows)
        _extend_meta(spec, quote, tokens)
        if tokens:
            meta = _load_meta(spec, quote) or {}
            have_lo, have_hi = meta.get('min_token'), meta.get('max_token')
        written += len(rows)
        if pages % 25 == 0:
            stamp = time.strftime('%Y-%m-%d %H:%M', time.gmtime(last_ts or 0))
            print(f'[dex_trades] {label} {pages} pages, {written} new, at {stamp}Z',
                  flush=True)
        if done or len(records) < _PAGE_LIMIT:
            break
        time.sleep(_PAGE_SLEEP)
    print(f'[dex_trades] {label} done: {pages} pages, {written} new rows', flush=True)
    return written


def backfill(days=30, spec='XLM', quote='USDC'):
    """Walk the tape backwards from now until `days` of history are on disk.

    Resumable and idempotent: it restarts from the oldest token already cached rather
    than from now, and rows inside the covered range are never written twice. Interrupt
    it and call it again.

    Cost is real and worth knowing before starting one: the pair does ~2,500 trades an
    hour including dust, so a day is ~60k rows and ~300 pages, and 30 days is ~1.8M rows
    (~200 MB) and ~9,000 requests. Seven days is the minimum the maker backtest's kill
    criterion needs.
    """
    cutoff = time.time() - float(days) * 86400
    meta = _load_meta(spec, quote)
    cursor = meta.get('min_token') if meta else None
    return _page_loop(spec, quote, cursor, 'desc',
                      lambda row: row['t'] < cutoff, f'backfill({days}d)')


def sync(spec='XLM', quote='USDC'):
    """Catch the cache up to now from the newest token already on disk.

    Cheap when run often, which is the point: this is what a recorder-style loop calls,
    and it keeps the tape usable as a live input rather than only as history.
    """
    meta = _load_meta(spec, quote)
    if not meta:
        return backfill(days=1, spec=spec, quote=quote)
    return _page_loop(spec, quote, meta['max_token'], 'asc',
                      lambda row: False, 'sync')


def read_cache(spec='XLM', quote='USDC'):
    """Every cached row, oldest first, as raw compact rows.

    Sorted on read because a backfill appends newest-first and a sync appends
    oldest-first, so the file is not in time order and never will be. Malformed lines are
    skipped for the reason market_recorder.read_history skips them.
    """
    path = _cache_path(spec, quote)
    if not path.exists():
        return []
    out = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    out.sort(key=lambda r: (r.get('t') or 0, _token_key(r.get('k'))))
    return out


def get_trades(spec='XLM', quote='USDC', start_ts=None, end_ts=None,
               min_usd=DUST_USD, sides_only=False, orderbook_only=True):
    """Cached trades in [start_ts, end_ts), oldest first.

    Trade = {'ts', 'price', 'base_amount', 'usd', 'taker_side', 'paging_token'}

    `price` and `usd` are in the quote asset, which for the only pair this is used on
    (XLM/USDC) is dollars. `taker_side` is 'buy' when asks were consumed, 'sell' when bids
    were, and None when the record could not say -- see `_taker_side`.

    `min_usd` drops dust at read time (see DUST_USD). `sides_only=True` additionally drops
    the rows with an unknown aggressor, which is what a fill model wants and what a volume
    total does not. `orderbook_only` (the default) drops liquidity-pool trades, which no
    resting offer could ever have filled.
    """
    out = []
    for row in read_cache(spec, quote):
        ts = row.get('t') or 0
        if orderbook_only and row.get('x'):
            continue
        if start_ts is not None and ts < start_ts:
            continue
        if end_ts is not None and ts >= end_ts:
            continue
        if (row.get('c') or 0) < min_usd:
            continue
        if sides_only and not row.get('s'):
            continue
        out.append(_expand(row))
    return out


def span(spec='XLM', quote='USDC'):
    """{'rows', 'first_ts', 'last_ts', 'days'} over the whole cache, dust included."""
    rows = read_cache(spec, quote)
    out = {'rows': len(rows), 'first_ts': None, 'last_ts': None, 'days': 0.0}
    if rows:
        out['first_ts'] = rows[0].get('t')
        out['last_ts'] = rows[-1].get('t')
        out['days'] = round((out['last_ts'] - out['first_ts']) / 86400.0, 2)
    return out


def tape_stats(hours=24, spec='XLM', quote='USDC'):
    """Trades/hour and volume/hour by size bucket over the last `hours`.

    The reason to bucket rather than report a mean: the size distribution is brutally
    skewed (median ~$0.005, max ~$490), so "2,500 trades an hour" is a statement about
    dust. The honest number for sizing a quote is the count in the bucket at or above the
    size you would actually rest, which is what the >= $1 and >= $4 rows say.
    """
    end = time.time()
    start = end - float(hours) * 3600
    rows = [r for r in read_cache(spec, quote) if start <= (r.get('t') or 0) < end]
    if not rows:
        return {'hours': hours, 'rows': 0}
    covered = max(1e-9, (rows[-1]['t'] - rows[0]['t']) / 3600.0)
    pool_usd = sum(r.get('c') or 0 for r in rows if r.get('x'))
    all_usd = sum(r.get('c') or 0 for r in rows)
    out = {'hours': hours, 'covered_hours': round(covered, 2), 'rows': len(rows),
           'pool_trades': sum(1 for r in rows if r.get('x')),
           'pool_volume_pct': round(100.0 * pool_usd / all_usd, 2) if all_usd else None,
           'buckets': {}, 'unknown_side_pct': None}
    # Buckets describe the CAPTURABLE tape only: a pool trade never touched the book.
    rows = [r for r in rows if not r.get('x')]
    if not rows:
        return out
    for floor in (0.0, 0.01, 1.0, 4.0, 20.0):
        sel = [r for r in rows if (r.get('c') or 0) >= floor]
        out['buckets'][f'>={floor:g}'] = {
            'trades': len(sel),
            'per_hour': round(len(sel) / covered, 1),
            'usd': round(sum(r.get('c') or 0 for r in sel), 2),
            'usd_per_hour': round(sum(r.get('c') or 0 for r in sel) / covered, 2),
        }
    sized = [r for r in rows if (r.get('c') or 0) >= DUST_USD]
    if sized:
        unknown = sum(1 for r in sized if not r.get('s'))
        out['unknown_side_pct'] = round(100.0 * unknown / len(sized), 2)
        buys = sum(1 for r in sized if r.get('s') == 'buy')
        out['taker_buy_pct'] = round(100.0 * buys / len(sized), 2)
    return out


def sync_daemon(interval=30, spec='XLM', quote='USDC'):
    """Keep the cache current forever. The single writer, mirroring market_recorder.daemon.

    One process does this for the whole population. Ten strategies each paging Horizon
    /trades on their own tick loop is the rate-limit incident market_recorder's
    single-writer rule already exists to prevent, and the tape is far chattier than the
    book -- ~2,700 trades an hour against 60 snapshots.

    Every iteration is individually guarded: this outlives any one monitor cycle, so one
    failed page must cost one interval and not the daemon.
    """
    while True:
        try:
            written = sync(spec, quote)
            covered = span(spec, quote)
            print(f'{time.time():.0f} +{written} rows, {covered["rows"]} cached, '
                  f'{covered["days"]}d', flush=True)
        except Exception as e:
            print(f'sync failed ({e})', flush=True)
        try:
            time.sleep(interval)
        except Exception:
            return


if __name__ == '__main__':
    import sys
    if '--sync-daemon' in sys.argv:
        every = 30
        if '--interval' in sys.argv:
            try:
                every = int(sys.argv[sys.argv.index('--interval') + 1])
            except Exception:
                pass
        print(f'syncing {_cache_path()} every {every}s', flush=True)
        sync_daemon(every)
    if '--backfill' in sys.argv:
        n = 30
        if '--days' in sys.argv:
            try:
                n = float(sys.argv[sys.argv.index('--days') + 1])
            except Exception:
                pass
        backfill(days=n)
    elif '--sync' in sys.argv:
        sync()
    print(json.dumps(span(), indent=2))
    stats = tape_stats(hours=24)
    print(json.dumps(stats, indent=2))

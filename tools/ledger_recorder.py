#!/usr/bin/env python3
"""The XLM/USDC order book as it actually was, every ledger, from ledger close meta.

WHY THIS EXISTS. MAKER_PHASE1.md's verdict is a *latency* result: the maker's edge lives
on a ridge at 5-6 bp half-width and decays to zero by 15 seconds of quote lag. Every
number behind that verdict was computed against `market_recorder`, which writes one book
snapshot every 60 seconds -- so the fill model has to interpolate across a window twelve
times longer than the effect it is measuring, and adverse selection has to be marked at
t+1/5/15 MINUTES because nothing finer exists. A conclusion about a 5-second phenomenon
drawn from 60-second data is a conclusion about the interpolator.

Stellar closes a ledger about every 5 seconds and `LedgerCloseMeta` carries every offer
that was created, modified or removed in it, with exact amounts and exact price rationals.
That is not a sample of the book, it is the book's derivative. Bootstrap the resting offer
set once and apply those deltas and you have the *exact* book at every close -- the
queue-ahead number the whole fill model rests on, measured rather than modelled.

WHAT IT IS NOT. This is not a second trade tape. `dex_trades.py` already holds the
executed tape with aggressor direction, backfilled from Horizon to 30 days, and it stays
the source for replay. Two things here are genuinely new: the book at 5-second resolution
(nothing else in this system has ever recorded it), and the RESTING OFFER ID behind every
fill, which `dex_trades._row` drops and which is the only way to check a queue-position
model against what actually got hit. Where the two disagree about a trade, dex_trades is
the one with 30 days of history; this one is the one that saw the ledger.

Both readings were checked against each other over one live 89-second window: dex_trades
reported $14,940.59 of volume and this module $14,940.72, agreeing to thirteen cents. The
COUNTS differ -- 104 against 208 -- and that is not a discrepancy: get_trades drops dust
and liquidity-pool flow by default, and this file keeps every atom on disk. Independently,
the maintained top of book matched Horizon's own order book to seven decimals on both
sides after twelve ledgers of deltas. Those two facts are what the price inversion in
_normalize and the claim extraction in _fill_from_atom rest on; re-run them before
trusting a change to either.

WHAT IT CANNOT DO. The RPC keeps a rolling window -- measured 2880 ledgers, about 4 hours
(`getHealth.ledgerRetentionWindow`). It is a live feed, not an archive: there is no
backfill here and there cannot be one. This history accrues in wall-clock time from the
moment the daemon starts, which is the reason to start it before it is needed rather than
when a replay wants it. Fall behind `oldestLedger` and the only recovery is to re-bootstrap
and accept the gap, which `daemon()` does and logs.

READ-ONLY, and outside the money boundary in the same sense `dex_trades` is: nothing here
places, cancels or prices an order, and it does not import `stellar_trader`. The USDC
issuer comes from `assets.py`, the tools-layer canonical source, and is never written out
again here.

COST, measured in the container against live ledgers rather than estimated: 1.2-2.0 MB of
XDR per ledger, 0.4-0.7 s to parse and 0.3-0.5 s to render as JSON, and 0.01 s to extract
the pair -- so about one second of CPU per five seconds of wall clock, and the extraction
is free. `_meta_json` is where that second is spent and where an optimisation would go; it
is written the simple way on purpose, because a background recorder that is 20% of one core
is cheap and a hand-rolled XDR walk that silently drifts from the schema is not.

Typical use:

    import sys; sys.path.append('/opt/tools')
    import ledger_recorder
    ledger_recorder.daemon()                      # the single writer
    rows = ledger_recorder.read_history(hours=2)  # book, one row per ledger
    ledger_recorder.tail(1)                       # the latest close, cheap

Configuration is environment, never argument: STELLAR_RPC_URL, and the bearer token from
STELLAR_RPC_TOKEN or the file named by STELLAR_RPC_TOKEN_FILE (default
/opt/.stellar_rpc.token). No token means `available()` is False and the daemon refuses to
start with a message, rather than spinning on 401s.
"""
import json
import os
import time
from pathlib import Path

import requests

import assets

RPC_URL = os.environ.get('STELLAR_RPC_URL', 'https://trade.6thstreetradio.org/rpc/')
TOKEN_FILE = Path(os.environ.get('STELLAR_RPC_TOKEN_FILE', '/opt/.stellar_rpc.token'))

_TIMEOUT = 45
_HORIZON = 'https://horizon.stellar.org'
_PAGE_LIMIT = 200               # Horizon's maximum for /offers

CACHE_DIR = Path('/opt/trades')
BOOK_PATH = CACHE_DIR / '.ledger_book_XLM_USDC.jsonl'
FILLS_PATH = CACHE_DIR / '.ledger_fills_XLM_USDC.jsonl'

# Two files rather than one row carrying both, because they have different value per byte
# and so want different retention. Measured on live ledgers, not estimated: a book row is
# ~1,285 B and there are ~17,300 ledgers a day, so the book costs ~22 MB/day, or ~155 MB
# for a week. Fills run 10-55 per ledger (~15 typical) at ~163 B, or ~42 MB/day, ~127 MB
# for three. The book is the dataset that exists nowhere else and is worth a week; the
# fills overlap dex_trades' 30-day tape and are worth keeping only long enough to validate
# a queue model against it.
BOOK_DAYS = 7
FILLS_DAYS = 3

# Deliberate OVER-estimates of one row, used only to size the read window in `tail`.
# Raising them is safe; the cost is reading a few more kilobytes from the end of a file.
_BOOK_ROW_BYTES = 2000
_FILL_ROW_BYTES = 260

# File sizes at which _trim stops being a no-op, expressed in BYTES and not derived from a
# row count. dex_trades._TRIM_BYTES has the same shape for the same reason: the guard
# exists so that the common case -- an append to a file that is nowhere near its retention
# window -- does not read the whole file, and it only works if it sits ABOVE the size the
# retained window actually occupies. Below that, every append rewrites the entire file
# forever and never gets back under the guard, which is the failure documented at
# market_recorder._ROW_BYTES.
#
# Deriving these from a row count is what makes that failure easy to reintroduce: the book
# writes exactly one row per ledger, but the fills file writes ~15, so a shared
# rows-per-ledger assumption is right for one file and 15x wrong for the other -- which is
# how a 127 MB file ends up being rewritten every five seconds. Set from the measured
# daily sizes above (22 MB/day for 7 days, 42 MB/day for 3) with headroom.
_BOOK_TRIM_BYTES = 200 * 1024 * 1024
_FILLS_TRIM_BYTES = 170 * 1024 * 1024

# Basis points from the touch at which cumulative depth is recorded. Taken from
# market_recorder so the two files are joinable column-for-column: a maker comparing its
# 60-second history against this 5-second one must not have to interpolate the depth grid
# as well as the time axis. The literal is the fallback for the case where market_recorder
# cannot be imported at all, and it must stay equal to that module's _CUM_BP.
try:
    from market_recorder import _CUM_BP as CUM_BP
except Exception:
    CUM_BP = (0.5, 1, 1.5, 2, 3, 4, 5, 7, 10, 15, 20, 30, 50, 75, 100, 150, 200)

# Raw levels kept per side, same count and same reason as market_recorder._LADDER_LEVELS:
# the structure of the touch, which on this pair is routinely dust resting a fraction of a
# basis point ahead of the real depth. Depth further out is covered exactly by CUM_BP.
LADDER_LEVELS = 5

# How often to throw the maintained book away and rebuild it from Horizon. Deltas are
# exact, so in principle this is never needed; in practice a dropped ledger, a Horizon
# page that straddled a close during the last bootstrap, or a protocol change to an entry
# type would all show up as a book that drifts from the real one and never comes back.
# Once an hour costs ~5 s and bounds how long a silent drift can persist. `_resync` logs
# the measured drift, which is the number worth watching -- a nonzero one is a bug here,
# not a market event.
RESYNC_LEDGERS = 720

# Offers that may differ between the maintained book and a fresh bootstrap before the
# difference is called drift rather than churn. Horizon has no consistent-snapshot read:
# paging ~4,300 offers takes about five seconds, during which the network closes a ledger
# roughly every five seconds and creates or removes 3-8 offers in this pair. So a clean
# resync legitimately shows a handful on each side -- measured 6 and 6 against a live
# book of 4,343 -- and a tolerance of zero would print an alarm every hour forever, which
# is worse than not checking at all. Above this, the difference is larger than churn can
# explain and means the delta application has a bug.
RESYNC_DRIFT_TOLERANCE = 40

# Between Horizon pages during a bootstrap. Horizon rate-limits by IP and this host also
# runs the live trader and dex_trades' sync daemon; see dex_trades._PAGE_SLEEP.
_PAGE_SLEEP = 0.12

POLL_INTERVAL = 2.0     # ledgers close every ~5 s; poll faster than that, fetch what is new
_MAX_BATCH = 5          # ledgers per getLedgers call while catching up


class LedgerRecorderError(RuntimeError):
    """Raised only by the explicit entry points (bootstrap, _rpc). The daemon catches
    everything -- a bad ledger must cost one row, not the recorder."""


# --------------------------------------------------------------------------------------
# RPC
# --------------------------------------------------------------------------------------

def _token():
    """The bearer token, or None. Environment first, then the file."""
    tok = os.environ.get('STELLAR_RPC_TOKEN')
    if tok and tok.strip():
        return tok.strip()
    try:
        tok = TOKEN_FILE.read_text().strip()
    except Exception:
        return None
    return tok or None


def available():
    """True if a token is configured. Checked before starting, so a missing token is a
    refusal with a message rather than a daemon spinning on 401s forever."""
    return _token() is not None


def _rpc(method, params=None):
    """One JSON-RPC call. Raises on transport failure or an `error` member."""
    tok = _token()
    if not tok:
        raise LedgerRecorderError(
            f'no RPC bearer token: set STELLAR_RPC_TOKEN or write {TOKEN_FILE}')
    resp = requests.post(
        RPC_URL,
        json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}},
        headers={'Authorization': f'Bearer {tok}'},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if 'error' in payload:
        raise LedgerRecorderError(f'RPC error from {method}: {payload["error"]}')
    return payload['result']


def health():
    """{'latestLedger', 'oldestLedger', ...}. `oldestLedger` is the retention floor and is
    the number that decides whether a resumed daemon can catch up or must re-bootstrap."""
    return _rpc('getHealth')


# --------------------------------------------------------------------------------------
# Asset identity. One pair, and it is checked rather than assumed.
# --------------------------------------------------------------------------------------

_USDC = ('USDC', assets.USDC_ISSUER)


def _tag(asset):
    """An XDR-JSON asset as 'native' or (code, issuer), or None if it is neither.

    Returning the issuer and not just the code is the whole point: an offer selling a
    look-alike USDC from some other issuer is a different market, and treating it as ours
    would put phantom depth in the queue-ahead number the fill model reads.
    """
    if asset == 'native':
        return 'native'
    if not isinstance(asset, dict):
        return None
    body = asset.get('credit_alphanum4') or asset.get('credit_alphanum12')
    if not isinstance(body, dict):
        return None
    return (body.get('asset_code'), body.get('issuer'))


def _pair_side(selling, buying):
    """'ask', 'bid', or None for an offer in some other market.

    An offer SELLING XLM for USDC is an ask (it offers XLM); one selling USDC for XLM is a
    bid. Nothing else in this pair exists, and everything else is not this pair.
    """
    s, b = _tag(selling), _tag(buying)
    if s == 'native' and b == _USDC:
        return 'ask'
    if s == _USDC and b == 'native':
        return 'bid'
    return None


# --------------------------------------------------------------------------------------
# The maintained book
# --------------------------------------------------------------------------------------

def _normalize(side, amount_raw, n, d):
    """(price in USDC per XLM, usd notional) for one resting offer, or None.

    Stellar's offer price is units of the BUYING asset per unit of the SELLING asset, and
    `amount` is denominated in the SELLING asset. So the two sides are not symmetric and
    the bid has to be inverted:

        ask (sell XLM, buy USDC): price = n/d is already USDC per XLM; amount is XLM
        bid (sell USDC, buy XLM): price = n/d is XLM per USDC, so invert; amount is USDC

    Getting this backwards does not raise anywhere -- it produces a book whose bid and ask
    are both plausible numbers on the wrong sides of the mid, which is the shape of a bug
    that survives a code review and dies in a backtest six weeks later.
    """
    try:
        n, d = int(n), int(d)
        amount = int(amount_raw) / 1e7
    except Exception:
        return None
    if n <= 0 or d <= 0 or amount <= 0:
        return None
    if side == 'ask':
        price = n / d
        return price, amount * price
    price = d / n
    return price, amount


def _offer_from_entry(offer):
    """One `offer` ledger entry as (offer_id, {'p','usd','seller'}), or None to skip."""
    side = _pair_side(offer.get('selling'), offer.get('buying'))
    if side is None:
        return None
    price = offer.get('price') or {}
    norm = _normalize(side, offer.get('amount'), price.get('n'), price.get('d'))
    if norm is None:
        return None
    return offer.get('offer_id'), {'side': side, 'p': norm[0], 'usd': norm[1],
                                   'seller': offer.get('seller_id')}


def bootstrap():
    """The full resting XLM/USDC offer set from Horizon, as {offer_id: entry}.

    Returns (book, seq) where `seq` is the RPC's latest ledger read BEFORE the first
    Horizon page. Before, not after, and that is the correctness argument for this whole
    module: paging ~4,300 offers takes about five seconds and certainly straddles a close,
    so the snapshot is not consistent with any single ledger. Replaying from the earlier
    sequence re-applies deltas that some pages already reflect, and re-applying is HARMLESS
    -- `created` and `updated` carry absolute post-state, and Stellar's offer ids come from
    a monotonic counter and are never reused, so a replayed `removed` cannot delete a
    different offer that later took the same id. Missing a delta is not harmless, which is
    why the error is taken in this direction.

    Measured on pubnet: ~2,800 asks and ~1,500 bids, 23 pages, ~5 s.
    """
    seq = int(_rpc('getLatestLedger')['sequence'])
    book = {}
    for side, params in (
        ('ask', {'selling_asset_type': 'native',
                 'buying_asset_type': 'credit_alphanum4',
                 'buying_asset_code': 'USDC',
                 'buying_asset_issuer': assets.USDC_ISSUER}),
        ('bid', {'selling_asset_type': 'credit_alphanum4',
                 'selling_asset_code': 'USDC',
                 'selling_asset_issuer': assets.USDC_ISSUER,
                 'buying_asset_type': 'native'}),
    ):
        cursor = None
        while True:
            query = dict(params, limit=_PAGE_LIMIT, order='asc')
            if cursor:
                query['cursor'] = cursor
            resp = requests.get(f'{_HORIZON}/offers', params=query, timeout=_TIMEOUT)
            resp.raise_for_status()
            records = (resp.json().get('_embedded') or {}).get('records') or []
            for rec in records:
                price = rec.get('price_r') or {}
                # Horizon reports `amount` in the selling asset as a decimal string, where
                # the ledger entry uses int64 units of 1e-7. Scale to match _normalize's
                # contract rather than giving it two input conventions.
                try:
                    raw = round(float(rec['amount']) * 1e7)
                except Exception:
                    continue
                norm = _normalize(side, raw, price.get('n'), price.get('d'))
                if norm is None:
                    continue
                book[rec['id']] = {'side': side, 'p': norm[0], 'usd': norm[1],
                                   'seller': rec.get('seller')}
            if len(records) < _PAGE_LIMIT:
                break
            cursor = records[-1]['paging_token']
            time.sleep(_PAGE_SLEEP)
    return book, seq


# --------------------------------------------------------------------------------------
# Ledger close meta -> deltas and fills
# --------------------------------------------------------------------------------------

def _meta_json(b64):
    """Base64 LedgerCloseMeta as plain JSON, with the version discriminant UNWRAPPED.

    The XDR renders as a tagged union -- {'v2': {...}} on protocol 27 -- and the caller
    wants the body. Unwrapping here rather than at the call site is not tidiness: a caller
    that reads `meta['tx_processing']` off the wrapper gets None from `.get`, applies no
    changes, finds no fills, and writes a book row that looks entirely plausible because
    the bootstrapped book is still in it. That is a recorder that runs for a week and
    records nothing, and it is the failure this function exists to make impossible.

    The one expensive call in this module (~0.4-0.7 s parse, ~0.3-0.5 s render, measured
    in the container). stellar_sdk is imported here rather than at module scope so that
    importing this file to read history -- which every reader does -- costs nothing and
    cannot fail on an interpreter without the SDK.
    """
    from stellar_sdk.xdr import LedgerCloseMeta
    body = json.loads(LedgerCloseMeta.from_xdr(b64).to_json())
    for version in ('v2', 'v1', 'v0'):
        inner = body.get(version)
        if isinstance(inner, dict):
            return inner
    raise LedgerRecorderError(f'unrecognised LedgerCloseMeta version: {list(body)}')


def _tx_result(tx):
    """The TransactionResultResult union of a transaction, unwrapping a fee bump.

    A fee-bump transaction reports as `txfee_bump_inner_success` (or `..._failed`) wrapping
    an InnerTransactionResultPair, and the operation results live one level further down.
    Measured on a live ledger: 313 transactions, of which 88 were fee-bump successes -- so
    treating the wrapper as "not a success" silently discards 28% of the ledger's offer
    activity, and the recorder still writes a perfectly plausible book row because the
    bootstrapped offers are all still in it. Wallets fee-bump routinely; this is the common
    case, not an edge one.
    """
    try:
        result = tx['result']['result']['result']
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    for wrapper in ('txfee_bump_inner_success', 'txfee_bump_inner_failed'):
        inner = result.get(wrapper)
        if isinstance(inner, dict):
            try:
                inner = inner['result']['result']
            except Exception:
                return None
            return inner if isinstance(inner, dict) else None
    return result


def _tx_meta(tx):
    """The operation-meta body of a transaction, or None.

    Only from a SUCCEEDED transaction. A failed one leaves no ledger-entry changes to
    apply, and its operation results carry no claims; taking its meta anyway is how a
    recorder ends up applying an offer state that the network rejected.
    """
    result = _tx_result(tx)
    if not isinstance(result, dict) or 'txsuccess' not in result:
        return None
    apply_meta = tx.get('tx_apply_processing') or {}
    for version in ('v4', 'v3', 'v2', 'v1'):
        body = apply_meta.get(version)
        if isinstance(body, dict) and body.get('operations') is not None:
            return body
    return None


def _op_results(tx):
    """The successful operation-result bodies of a transaction, innermost union first."""
    result = _tx_result(tx)
    if not isinstance(result, dict):
        return
    for entry in result.get('txsuccess') or []:
        inner = entry.get('opinner') if isinstance(entry, dict) else None
        if not isinstance(inner, dict):
            continue
        for op_type, body in inner.items():
            if isinstance(body, dict):
                yield op_type, body


def _claims(tx):
    """Every ClaimAtom in a transaction, from offer ops and path payments alike.

    Offer ops report claims as `success.offers_claimed`; path payments as
    `success.offers`. Both carry the same atom, and a maker is filled by either -- a path
    payment that routes through the order book consumes resting offers exactly as a
    crossing offer does. Reading only the offer ops would silently under-count fills, and
    on this pair path payments are a real fraction of the flow.
    """
    for op_type, body in _op_results(tx):
        success = body.get('success')
        if not isinstance(success, dict):
            continue
        atoms = success.get('offers_claimed')
        if atoms is None:
            atoms = success.get('offers')
        for atom in atoms or []:
            if isinstance(atom, dict):
                yield op_type, atom


def _fill_from_atom(atom):
    """One ClaimAtom as a fill row, or None.

    The `order_book` variant only. A `liquidity_pool` atom is a trade against an AMM, and
    no resting offer anywhere could have captured it -- MAKER_PHASE1 measured that flow at
    9.25% of volume, against MAKER.md's assumption of zero, so counting it here would
    inflate the one number the maker's sizing is derived from.

    `side` is the side of the BOOK that was consumed, not the aggressor's direction: 'ask'
    means a resting ask was lifted, so the aggressor bought XLM. That convention is the
    opposite of dex_trades' `taker_side` and is named `side` rather than `taker_side` for
    exactly that reason -- see _taker_side in that module for what happens when the two are
    confused.
    """
    body = atom.get('order_book')
    if not isinstance(body, dict):
        return None
    sold, bought = _tag(body.get('asset_sold')), _tag(body.get('asset_bought'))
    if sold == 'native' and bought == _USDC:
        side = 'ask'
        xlm_raw, usd_raw = body.get('amount_sold'), body.get('amount_bought')
    elif sold == _USDC and bought == 'native':
        side = 'bid'
        xlm_raw, usd_raw = body.get('amount_bought'), body.get('amount_sold')
    else:
        return None
    try:
        xlm, usd = int(xlm_raw) / 1e7, int(usd_raw) / 1e7
    except Exception:
        return None
    if xlm <= 0 or usd <= 0:
        # Stellar emits zero-amount atoms for offers that were crossed but consumed
        # nothing. They are not fills and a price cannot be derived from them.
        return None
    return {'oid': body.get('offer_id'), 'sel': body.get('seller_id'), 'side': side,
            'xlm': round(xlm, 7), 'usd': round(usd, 7), 'p': usd / xlm}


def apply_ledger(book, meta):
    """Apply one ledger's offer changes to `book` in place. Returns (fills, counts).

    Walks operation meta for entry changes and operation results for claims. The two are
    independent readings of the same event and are deliberately not derived from each
    other: the changes say what the book looks like NOW, the claims say what traded and
    against WHICH resting offer. A fill model needs both, and computing one from the other
    would lose the offer id that makes the queue model checkable.
    """
    fills, counts = [], {'created': 0, 'updated': 0, 'removed': 0}
    for tx in meta.get('tx_processing') or []:
        body = _tx_meta(tx)
        if body is not None:
            for op in body.get('operations') or []:
                for change in op.get('changes') or []:
                    if not isinstance(change, dict) or not change:
                        continue
                    kind = next(iter(change))
                    payload = change[kind]
                    if not isinstance(payload, dict):
                        continue
                    if kind == 'removed':
                        key = payload.get('offer')
                        # A LedgerKey, not an entry: it carries seller and offer id and
                        # nothing else, so which market it belonged to is unknowable here.
                        # Discard by id and let the miss be a no-op -- ids are globally
                        # unique, so removing one we never held cannot remove a real one.
                        if isinstance(key, dict):
                            if book.pop(key.get('offer_id'), None) is not None:
                                counts['removed'] += 1
                        continue
                    if kind not in ('created', 'updated'):
                        continue          # 'state' is the pre-image; the post-image follows
                    data = payload.get('data')
                    if not isinstance(data, dict) or 'offer' not in data:
                        continue
                    parsed = _offer_from_entry(data['offer'])
                    if parsed is None:
                        continue
                    book[parsed[0]] = parsed[1]
                    counts[kind] += 1
        for _op_type, atom in _claims(tx):
            fill = _fill_from_atom(atom)
            if fill is not None:
                fills.append(fill)
    return fills, counts


# --------------------------------------------------------------------------------------
# The row
# --------------------------------------------------------------------------------------

def _levels(book, side):
    """Resting size aggregated per price on one side, best first.

    Per price and not per offer: a quote joining a price level goes to the BACK of the
    queue already resting there, so same-price size is ahead of it and has to be summed,
    not listed.
    """
    by_price = {}
    for entry in book.values():
        if entry['side'] != side:
            continue
        by_price[entry['p']] = by_price.get(entry['p'], 0.0) + entry['usd']
    return sorted(by_price.items(), reverse=(side == 'bid'))


def _cum(levels, touch, side):
    """[[bp, usd], ...]: everything resting at or better than each CUM_BP from the touch.

    A list of pairs rather than a dict so it survives a JSON round trip (a dict would key
    on stringified floats), matching market_recorder._cum_depth exactly so the two files
    join. "At or better than" includes the touch price itself, for the queue reason in
    _levels.
    """
    if not levels or not touch or touch <= 0:
        return None
    out = []
    for bp in CUM_BP:
        edge = touch * (1 - bp / 10000.0) if side == 'bid' else touch * (1 + bp / 10000.0)
        total = sum(usd for price, usd in levels
                    if (price >= edge if side == 'bid' else price <= edge))
        out.append([bp, round(total, 2)])
    return out


def book_row(book, seq, close_time, fills, counts):
    """One ledger's book as the row written to BOOK_PATH.

    Prices are NOT rounded, for market_recorder's reason: a book 10 bp wide has meaning in
    the sixth decimal of an XLM price. Sizes are rounded to 4 dp rather than 2 because the
    top of this book is routinely half-cent dust, and rounding that to $0.00 reports zero
    size queued ahead of a quote placed there -- an error in the optimistic direction on
    the one number the fill model is built on.
    """
    bids, asks = _levels(book, 'bid'), _levels(book, 'ask')
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    row = {
        'ts': float(close_time) if close_time else time.time(),
        'seq': int(seq),
        'bid': best_bid,
        'ask': best_ask,
        'offers': len(book),
        'created': counts.get('created', 0),
        'updated': counts.get('updated', 0),
        'removed': counts.get('removed', 0),
    }
    if best_bid and best_ask and best_ask > 0:
        mid = (best_bid + best_ask) / 2.0
        row['mid'] = mid
        # `is not None` downstream, not a truth test: a locked book (bid == ask) has a real
        # spread of 0.0 and recording that as None is what makes a reader's
        # `float(row.get('spread_bp', 0.0))` raise instead of taking its default.
        row['spread_bp'] = round((best_ask - best_bid) / mid * 10000, 4)
    else:
        row['mid'] = None
        row['spread_bp'] = None
    row['bids'] = [{'p': p, 'usd': round(usd, 4)} for p, usd in bids[:LADDER_LEVELS]]
    row['asks'] = [{'p': p, 'usd': round(usd, 4)} for p, usd in asks[:LADDER_LEVELS]]
    row['bid_cum'] = _cum(bids, best_bid, 'bid')
    row['ask_cum'] = _cum(asks, best_ask, 'ask')
    # Aggregate fill flow, so the commonest question -- how much got consumed on each side
    # this close -- is answerable from the book file alone, without joining the fills file.
    row['fill_n'] = len(fills)
    row['fill_bid_usd'] = round(sum(f['usd'] for f in fills if f['side'] == 'bid'), 4)
    row['fill_ask_usd'] = round(sum(f['usd'] for f in fills if f['side'] == 'ask'), 4)
    return row


def _fill_rows(fills, seq, close_time):
    """Fills as the rows written to FILLS_PATH.

    Short keys because there are ~15 of these a ledger and ~17,300 ledgers a day, so ten
    bytes of key names is ~2.5 MB a day. The seller address is more than a third of the
    row and is kept anyway: adverse selection is the number that decides whether this
    domain works at all, and "are the fills that go against us concentrated in a handful of
    counterparties?" is a question that cannot be asked of a file that dropped the
    counterparty. Its cost is stated at FILLS_DAYS rather than hidden.
    """
    ts = float(close_time) if close_time else time.time()
    return [{'t': round(ts, 3), 'q': int(seq), 'o': f['oid'], 'a': f['sel'],
             's': f['side'], 'x': f['xlm'], 'u': f['usd']} for f in fills]


# --------------------------------------------------------------------------------------
# Persistence. Append-only JSONL, one writer, many readers -- market_recorder's contract.
# --------------------------------------------------------------------------------------

def _layout(path):
    """(timestamp key, ledger-sequence key, row-size estimate, trim threshold) for a file.

    Keyed on the FILENAME rather than on identity with the module-level BOOK_PATH, so a
    caller that repointed CACHE_DIR -- a test, or a second pair some day -- still gets the
    right column names instead of silently being treated as the book.
    """
    if path.name == FILLS_PATH.name:
        return 't', 'q', _FILL_ROW_BYTES, _FILLS_TRIM_BYTES
    return 'ts', 'seq', _BOOK_ROW_BYTES, _BOOK_TRIM_BYTES


def _append(path, rows, days):
    if not rows:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    _trim(path, days)


def _trim(path, days):
    """Drop rows older than `days`. Rewrites via a temp file so a reader mid-trim sees
    either the old file or the new one, never a truncated one.

    Guarded by a cheap size check first, because the alternative is reading a
    hundred-megabyte file on every ledger. See _BOOK_TRIM_BYTES for why that guard is a
    byte count and not a row count.
    """
    try:
        if not path.exists():
            return
        stamp, _seq_key, _row_bytes, trim_bytes = _layout(path)
        if os.path.getsize(path) < trim_bytes:
            return
        cutoff = time.time() - days * 86400
        kept = []
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get(stamp, 0) >= cutoff:
                        kept.append(line)
                except Exception:
                    continue
        tmp = path.with_suffix('.tmp')
        tmp.write_text('\n'.join(kept) + ('\n' if kept else ''))
        tmp.replace(path)
    except Exception:
        pass


def tail(n=1, path=None, max_bytes=262144):
    """The last `n` rows of a file, oldest first, without reading the whole thing.

    THE ONLY function here a trading loop may call. read_history scans every line of a file
    that grows by ~17,300 rows a day; on a tick loop that is a linear scan per tick per
    strategy. This seeks from EOF instead.

    The first fragment of the read window is dropped unless the window covers the whole
    file: an arbitrary byte offset lands mid-line, and that partial line is a slice artifact
    to be discarded, not a torn write to be tolerated. Malformed lines are skipped for the
    reason market_recorder skips them -- a daemon appends here, so a torn final line is a
    normal state.
    """
    path = path or BOOK_PATH
    try:
        if not path.exists():
            return []
        size = os.path.getsize(path)
        _stamp, _seq_key, row_bytes, _trim_bytes = _layout(path)
        want = max(4096, min(int(max_bytes), max(1, int(n)) * row_bytes * 4))
        start = max(0, size - want)
        with path.open('rb') as handle:
            handle.seek(start)
            chunk = handle.read()
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


def read_history(hours=2, path=None):
    """Rows from the last `hours`, oldest first. [] if there are none.

    Default 2 hours, not market_recorder's 168: at one row per 5 seconds a week is 1.2
    million rows, and a caller that wanted `hours=168` from a 60-second file will silently
    ask for 12x the memory here. Ask for what you need.
    """
    path = path or BOOK_PATH
    if not path.exists():
        return []
    stamp, _seq_key, _row_bytes, _trim_bytes = _layout(path)
    cutoff = time.time() - float(hours) * 3600
    out = []
    try:
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get(stamp, 0) >= cutoff:
                    out.append(row)
    except Exception:
        return out
    return out


def span(path=None):
    """{'rows', 'first_ts', 'last_ts', 'hours', 'gaps'} over a whole file.

    `gaps` counts breaks in the ledger sequence, which is the health number that matters
    for the BOOK: that file has exactly one row per ledger, this feed has a 4-hour
    retention window and no backfill, so a daemon that was down leaves a hole that can
    never be filled and a replay silently spanning it is wrong.

    It is None for the fills file, deliberately. That file skips any ledger in which
    nothing traded, so a sequence break there is the ordinary quiet market and counting it
    as a gap reports data loss that did not happen -- a health number that cries wolf gets
    ignored, and then it is not a health number. Check the book's gaps; the two files are
    written in the same loop and a hole in one is a hole in both.
    """
    path = path or BOOK_PATH
    stamp, seq_key, _row_bytes, _trim_bytes = _layout(path)
    tracks_gaps = path.name != FILLS_PATH.name
    out = {'rows': 0, 'first_ts': None, 'last_ts': None, 'hours': 0.0,
           'gaps': 0 if tracks_gaps else None}
    prev = None
    try:
        if not path.exists():
            return out
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ts = row.get(stamp)
                if not ts:
                    continue
                out['rows'] += 1
                if out['first_ts'] is None:
                    out['first_ts'] = ts
                out['last_ts'] = ts
                if tracks_gaps:
                    seq = row.get(seq_key)
                    if prev is not None and seq is not None and seq > prev + 1:
                        out['gaps'] += 1
                    if seq is not None:
                        prev = seq
    except Exception:
        pass
    if out['first_ts'] and out['last_ts']:
        out['hours'] = round((out['last_ts'] - out['first_ts']) / 3600.0, 3)
    return out


# --------------------------------------------------------------------------------------
# The daemon
# --------------------------------------------------------------------------------------

def _fetch(start, limit):
    """`limit` ledgers from `start`, as [(seq, close_time, meta_json)]."""
    result = _rpc('getLedgers', {'startLedger': int(start),
                                 'pagination': {'limit': int(limit)},
                                 'xdrFormat': 'base64'})
    out = []
    for entry in result.get('ledgers') or []:
        try:
            out.append((int(entry['sequence']), entry.get('ledgerCloseTime'),
                        _meta_json(entry['metadataXdr'])))
        except Exception as exc:
            print(f'ledger {entry.get("sequence")} undecodable ({exc})', flush=True)
    return out


def _resync(book):
    """Rebuild from Horizon and report the difference against the maintained book.

    This is the self-check this module has instead of a test against live data, and its
    limit is worth stating plainly: the two readings are seconds apart and Horizon cannot
    be read as a consistent snapshot, so a small difference is churn and proves nothing
    either way. Only a difference above RESYNC_DRIFT_TOLERANCE is evidence of a bug, and
    that is the only case that shouts. Returns (book, seq).

    The stronger check is the one in the validation run rather than here: the maintained
    top of book matched Horizon's own order book to seven decimals on both sides after
    twelve ledgers of deltas. Compare tops, not sets, when something looks wrong.
    """
    fresh, seq = bootstrap()
    added = len(set(fresh) - set(book))
    dropped = len(set(book) - set(fresh))
    level = 'RESYNC DRIFT' if max(added, dropped) > RESYNC_DRIFT_TOLERANCE else 'resync'
    print(f'{level}: {added} offers missing from maintained book, {dropped} stale in it '
          f'(of {len(fresh)}); rebuilt at ledger {seq}', flush=True)
    return fresh, seq


def daemon(poll=POLL_INTERVAL):
    """Record every ledger, forever. The single writer for both files.

    Structure follows market_recorder.daemon: every iteration is individually guarded,
    because this process is supervised by monitor but outlives a single monitor cycle and
    one bad ledger must cost one row.

    The one case that is NOT survivable in place is falling behind the RPC's retention
    floor -- there is no backfill, so those ledgers are gone. That is handled by
    re-bootstrapping and logging the gap rather than by pretending the sequence is
    continuous; `span()['gaps']` is where it shows up afterwards.
    """
    if not available():
        print(f'no RPC bearer token: set STELLAR_RPC_TOKEN or write {TOKEN_FILE}',
              flush=True)
        return
    book, cursor = bootstrap()

    # Every bootstrap rewinds the cursor to before its own Horizon paging (see bootstrap's
    # docstring: replaying a delta is harmless, missing one is not), and a restart rewinds
    # it again. Re-APPLYING a ledger is exactly what that design wants; re-WRITING its row
    # is not, because a duplicate row is a second observation of a moment that only
    # happened once, and every downstream average over these rows would double-count it.
    # So the rewind is kept and the write is gated on this instead. Seeded from what is
    # already on disk so a restart cannot duplicate the previous run's tail either.
    last_row = tail(1)
    last_written = last_row[0].get('seq', 0) if last_row else 0
    print(f'bootstrapped {len(book)} offers, replaying from ledger {cursor}'
          f'{f" (already recorded through {last_written})" if last_written else ""}',
          flush=True)
    since_resync = 0
    while True:
        try:
            info = health()
            latest = int(info['latestLedger'])
            oldest = int(info['oldestLedger'])
            if cursor < oldest:
                print(f'fell behind retention ({cursor} < {oldest}); '
                      f'{oldest - cursor} ledgers lost, re-bootstrapping', flush=True)
                book, cursor = bootstrap()
                since_resync = 0
                continue
            if cursor > latest:
                time.sleep(poll)
                continue
            for seq, close_time, meta in _fetch(cursor, min(_MAX_BATCH, latest - cursor + 1)):
                fills, counts = apply_ledger(book, meta)
                if seq > last_written:
                    _append(BOOK_PATH, [book_row(book, seq, close_time, fills, counts)],
                            BOOK_DAYS)
                    _append(FILLS_PATH, _fill_rows(fills, seq, close_time), FILLS_DAYS)
                    last_written = seq
                cursor = seq + 1
                since_resync += 1
            if since_resync >= RESYNC_LEDGERS:
                book, resync_seq = _resync(book)
                cursor = min(cursor, resync_seq)
                since_resync = 0
        except Exception as exc:
            print(f'poll failed ({exc})', flush=True)
            time.sleep(poll * 2)
            continue
        time.sleep(poll)


def summary(hours=1):
    """Min/mean/max of the interesting columns over `hours`, for a log line."""
    rows = read_history(hours)
    if not rows:
        return None
    out = {'rows': len(rows), 'hours': hours,
           'fills': sum(r.get('fill_n', 0) for r in rows),
           'fill_usd': round(sum(r.get('fill_bid_usd', 0) + r.get('fill_ask_usd', 0)
                                 for r in rows), 2)}
    for field in ('spread_bp', 'offers'):
        values = [r[field] for r in rows if r.get(field) is not None]
        if values:
            out[field] = {'min': min(values),
                          'mean': round(sum(values) / len(values), 3),
                          'max': max(values)}
    return out


if __name__ == '__main__':
    import sys

    if '--daemon' in sys.argv:
        every = POLL_INTERVAL
        if '--poll' in sys.argv:
            try:
                every = float(sys.argv[sys.argv.index('--poll') + 1])
            except Exception:
                pass
        print(f'recording {BOOK_PATH} and {FILLS_PATH}, polling every {every}s', flush=True)
        daemon(every)
    elif '--span' in sys.argv:
        print('book: ', json.dumps(span(BOOK_PATH)))
        print('fills:', json.dumps(span(FILLS_PATH)))
        print('last: ', json.dumps(summary(), indent=2))
    elif '--once' in sys.argv:
        # One bootstrap and one ledger, printed and not written. The cheap check that the
        # token, the RPC, the pair filter and the price inversion all work on this host.
        held, seq = bootstrap()
        print(f'bootstrapped {len(held)} offers at ledger {seq}')
        for sequence, close_time, meta in _fetch(seq, 1):
            filled, counted = apply_ledger(held, meta)
            print(json.dumps(book_row(held, sequence, close_time, filled, counted),
                             indent=2))
            print(f'fills: {len(filled)}')
            for fill in filled[:5]:
                print('  ', json.dumps(fill))
    else:
        print(__doc__)
        print(f'token configured: {available()}   rpc: {RPC_URL}')
        print('book: ', json.dumps(span(BOOK_PATH)))
        print('fills:', json.dumps(span(FILLS_PATH)))

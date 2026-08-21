#!/usr/bin/env python3
"""Real P&L for the `claudio` pubnet account, reconstructed from Horizon's own ledger.

This is the independent, un-gameable counterpart to /opt/pubnet_tally.py. That script
reads *.pubnet.log -- this system's own record of what it *believes* it submitted, with
amount_xlm "estimated from the pre-trade price, not the exact fill". This one reads the
public ledger: every balance change Horizon reports for the account, whoever caused it,
including trades this system never logged, operator deposits, unsolicited payments and
transaction fees. Nothing written by a strategy is an input, so no revision can flatter
the number.

The window starts at `baseline_at` in /opt/trades/.short_buffer.json -- the moment an
operator last hand-reconciled the real account (see pubnet_tally._read_short_buffer).
`baseline_xlm` in that same file is NOT used as the opening balance: it is a derived
offset (real_balance - short_buffer - trade_log_qty), not a raw balance. The opening
balances here are reconstructed from the ledger instead -- current balances minus every
delta since baseline_at -- and baseline_xlm is printed alongside only as a cross-check.

WHAT COUNTS AS P&L

    P&L = portfolio value now
        - portfolio value at baseline
        - net capital contributed since baseline

Capital is money that moved in or out of the account without being traded for: the
operator's 60 XLM short buffer, ordinary payments, claimed claimable balances. Those
change net worth without being earnings, so they are subtracted. Transaction fees are
NOT capital -- they are a real cost and stay inside P&L (reported as a memo line so
they are visible rather than buried in the XLM leg).

Per asset the same identity decomposes exactly:

    pnl(a) = value_end(a) - value_start(a) - capital_net(a) + cash_flow(a)

where cash_flow(a) is the USD the asset generated through swaps (positive when sold,
negative when bought). Summed over every asset the cash_flow terms cancel -- each swap
credits one leg exactly what it debits the other -- and what is left is the headline.
USDC is held at $1 by definition (it is the settlement asset, not a position), so
pnl(USDC) comes out at exactly zero and is a live check on the arithmetic, not a result.

The avg-cost realized/unrealized split printed beside it is a second, independently
computed view: it answers "did the round trips make money" rather than "did net worth
go up", which for an XLM-heavy account are very different questions. The two are
reconciled explicitly on a `resid` column rather than being assumed to agree.

DIRECTION SEMANTICS, verified against live records rather than assumed:

- account_credited / account_debited effects are on the account itself and are
  unambiguous. They are the primary source: for a multi-hop path payment they collapse
  the whole path to its true net (-source_asset, +dest_asset), which is what a position
  ledger wants. The intermediate hops are assets the account never held a trustline for
  and never really owned.
- A `trade` effect's sold_/bought_ fields are from the perspective of the effect's own
  `account`. A `liquidity_pool_trade` effect's sold/bought are from the POOL's
  perspective -- the opposite. These are only consulted for an operation that produced
  no credit/debit effects, which is what a resting maker offer being taken looks like
  (stellar_trader.place_offer); every taker path payment is covered by the first source.
- The /trades endpoint is deliberately NOT used. It reports each hop of a path payment
  separately, and its base_is_seller flag does not carry the same meaning for a
  liquidity_pool hop as for an orderbook one -- reading it as if it did credits the
  account with hundreds of thousands of units of assets it never owned.

Deliberately lives outside /opt/tools and /opt/master_agent, same as pubnet_tally.py:
those are the two directories monitor.check_boundary_integrity() watches, and a
read-only report has no business halting live trading every time its output changes.

  python3 /opt/pubnet_summary.py                 # the report
  python3 /opt/pubnet_summary.py --trades        # + every balance-changing operation
  python3 /opt/pubnet_summary.py --json          # machine-readable
  python3 /opt/pubnet_summary.py --since 2026-08-15T00:00:00Z   # override the baseline
  python3 /opt/pubnet_summary.py --account G...  # a different account
"""
import argparse
import bisect
import calendar
import collections
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HORIZON = 'https://horizon.stellar.org'
IDENTITY = 'claudio'
# Fallback only. Resolved from the `stellar` CLI first, so this cannot silently report on
# the wrong account if the identity is ever re-keyed; see stellar_trader.py's docstring,
# which records the same address as verified against a plain Horizon GET.
FALLBACK_ADDRESS = 'GBTFQJ6VARJYI2C6JLPUXQ4CAKRNJF3KEYXXJ5T74DV47RSSNIJCH5VM'

TRADES_DIR = Path('/opt/trades')
SHORT_BUFFER_PATH = TRADES_DIR / '.short_buffer.json'
TOOLS_DIR = '/opt/tools'

# The settlement asset. stellar_trader denominates every trade against it and wind_down
# flattens into it, so it is treated as cash at $1 rather than as a position to be marked.
USDC_ISSUER = 'GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN'
USDC = f'USDC:{USDC_ISSUER}'

PAGE = 200
TIMEOUT = 40
RETRIES = 4
# Bounds the walk when a caller points --since a long way back. Each page is one HTTP
# round trip against a public endpoint; a runaway would rate-limit the same Horizon the
# live trader depends on. Truncation is reported, never silent.
MAX_PAGES = 400

STROOP = 1e-7


# --- horizon ------------------------------------------------------------------------

def _get(url):
    """GET with backoff. Horizon 429s, and this walks a few hundred pages of it while a
    live trader is using the same host -- failing the whole report on one throttle would
    make it useless exactly when the system is busiest."""
    delay = 1.0
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or attempt == RETRIES - 1:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == RETRIES - 1:
                raise
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f'unreachable: {url}')


def _paged_since(path, params, since, ts_key='created_at'):
    """Walk a Horizon collection newest-first and stop at the first record older than
    `since`. Descending is the point: the cost is proportional to the window asked for,
    not to the account's whole history.

    Returns (records_oldest_first, truncated).
    """
    out = []
    cursor = None
    for _ in range(MAX_PAGES):
        q = dict(params, order='desc', limit=PAGE)
        if cursor:
            q['cursor'] = cursor
        page = _get(f'{HORIZON}{path}?' + urllib.parse.urlencode(q))
        records = page.get('_embedded', {}).get('records', [])
        if not records:
            out.reverse()
            return out, False
        for r in records:
            if _epoch(r[ts_key]) < since:
                out.reverse()
                return out, False
            out.append(r)
        cursor = records[-1]['paging_token']
    out.reverse()
    return out, True


def _epoch(iso):
    """Horizon stamps are UTC. calendar.timegm, not time.mktime -- mktime reads its
    argument as local time and shifts by the running DST offset, which would slide the
    whole window by an hour twice a year."""
    return calendar.timegm(time.strptime(iso, '%Y-%m-%dT%H:%M:%SZ'))


def _fmt_ts(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else '?'


def resolve_address(override=None):
    if override:
        return override, 'command line'
    try:
        result = subprocess.run(['stellar', 'keys', 'address', IDENTITY],
                                capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), f'`stellar keys address {IDENTITY}`'
    except Exception:
        pass
    return FALLBACK_ADDRESS, 'hardcoded fallback (stellar CLI unavailable)'


def _spec(obj, prefix=''):
    """Canonical 'XLM' or 'CODE:ISSUER' out of Horizon's asset_type/code/issuer triple."""
    if obj.get(prefix + 'asset_type') == 'native':
        return 'XLM'
    code, issuer = obj.get(prefix + 'asset_code'), obj.get(prefix + 'asset_issuer')
    return f'{code}:{issuer}' if code and issuer else 'XLM'


def _spec_from_string(s):
    """The 'native' / 'CODE:ISSUER' form used inside liquidity_pool_trade effects."""
    return 'XLM' if s == 'native' else s


# --- inputs -------------------------------------------------------------------------

def read_baseline():
    """baseline_at / baseline_xlm / funded_xlm out of .short_buffer.json.

    Only baseline_at is load-bearing here -- it is the window start the whole report is
    anchored on. baseline_xlm is carried for display: it is pubnet_tally's hand-set
    reconciliation offset, not a raw balance, so it is shown next to this script's own
    reconstructed opening balance rather than substituted for it.
    """
    try:
        record = json.loads(SHORT_BUFFER_PATH.read_text())
    except Exception as e:
        return {'baseline_at': None, 'baseline_xlm': None, 'funded_xlm': 0.0,
                'funded_at': None, 'error': str(e)}
    baseline_xlm = record.get('baseline_xlm')
    return {
        'baseline_at': record.get('baseline_at'),
        'baseline_xlm': float(baseline_xlm) if baseline_xlm is not None else None,
        'funded_xlm': float(record.get('funded_xlm', 0.0) or 0.0),
        'funded_at': record.get('funded_at'),
        'error': None,
    }


def parse_since(text):
    """--since as an epoch or an ISO-8601 UTC stamp."""
    try:
        return float(text)
    except ValueError:
        pass
    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return calendar.timegm(time.strptime(text, fmt))
        except ValueError:
            continue
    raise SystemExit(f'could not parse --since {text!r}: want an epoch or 2026-08-11T20:01:43Z')


# --- prices -------------------------------------------------------------------------

class Prices:
    """XLM/USD now and at a past instant, plus depth-capped marks for everything else.

    Every source here is optional. This is a read-only report that must still produce
    the ledger half of its output on a host with no /opt/tools -- an unpriceable leg is
    reported as unpriced, the way score.py treats one, never silently valued at zero.
    """

    def __init__(self):
        self.notes = []
        self._candles = []
        self._candle_ts = []
        self._spot = None
        self._marks = {}
        if TOOLS_DIR not in sys.path:
            sys.path.append(TOOLS_DIR)
        try:
            import ohlc_history
            self._candles = ohlc_history.get_candles(hours=720, interval=60)
            self._candle_ts = [c['ts'] for c in self._candles]
        except Exception as e:
            self.notes.append(f'no XLM candle history ({e}); historical XLM marked at spot')
        try:
            import price_feed
            self._spot = price_feed.get_price()
        except Exception as e:
            self.notes.append(f'no live XLM price ({e})')
        if self._spot is None and self._candles:
            self._spot = self._candles[-1]['close']
            self.notes.append('live XLM price unavailable; using the newest candle close')

    def check_window(self, since):
        """Warn when the window starts before the candle history reaches. xlm_at would
        otherwise pin the opening mark to the oldest candle it has and report a confident
        number for a price it never saw -- which lands squarely on value_start, and so on
        the headline."""
        if self._candle_ts and since < self._candle_ts[0]:
            self.notes.append(
                f'window starts {(self._candle_ts[0] - since) / 86400:.1f} days before the '
                f'oldest XLM candle; the opening mark is that candle, not the real price')

    def xlm_at(self, ts):
        """Close of the hourly candle covering `ts`, else spot. A candle is ~an hour wide
        and the caps make every trade small, so this is accurate enough to value a
        capital flow or an opening inventory -- it is never used to price a fill, which
        always carries its own realized rate."""
        if self._candle_ts:
            i = bisect.bisect_right(self._candle_ts, ts) - 1
            if 0 <= i < len(self._candles):
                return self._candles[i]['close']
            if ts < self._candle_ts[0]:
                return self._candles[0]['close']
        return self._spot

    def spot_xlm(self):
        return self._spot

    def unit_now(self, spec):
        """Current USD per unit. None when the asset cannot be priced at all."""
        if spec == USDC:
            return 1.0
        if spec == 'XLM':
            return self._spot
        if spec in self._marks:
            return self._marks[spec]
        mark = None
        try:
            import dex_price
            mark = dex_price.get_mark(spec)
        except Exception:
            mark = None
        self._marks[spec] = mark
        return mark

    def value_now(self, spec, amount):
        """USD the holding could actually be realized for. Non-XLM legs go through
        dex_price.mark_value, which walks the real bid ladder and marks anything past
        available depth at zero -- the same rule score.py ranks on, so a position that
        only exists at the mid does not show up here as money."""
        if amount == 0:
            return 0.0, True
        if spec == USDC:
            return amount, True
        if spec != 'XLM':
            try:
                import dex_price
                realized = dex_price.mark_value(spec, amount)
                if realized is not None:
                    return realized, True
            except Exception:
                pass
        unit = self.unit_now(spec)
        if unit is None:
            return 0.0, False
        return amount * unit, True


# --- ledger construction ------------------------------------------------------------

# Operation types that move value across the account boundary without it having been
# traded for. These are capital, not earnings, and are subtracted from the headline.
CAPITAL_TYPES = {
    'payment', 'create_account', 'account_merge',
    'claim_claimable_balance', 'create_claimable_balance',
    'path_payment_strict_send', 'path_payment_strict_receive',
}


def classify(op, address):
    """'swap' | 'capital' | 'other' for one operation.

    A path payment is only a swap when it is a self-payment (from == to == us), which is
    exactly the shape stellar_trader._swap submits. The same operation aimed at somebody
    else -- or arriving from them -- moved value across the boundary and is capital.
    """
    op_type = op.get('type')
    if op_type in ('path_payment_strict_send', 'path_payment_strict_receive'):
        if op.get('from') == address and op.get('to') == address:
            return 'swap'
        return 'capital'
    if op_type in ('manage_buy_offer', 'manage_sell_offer', 'create_passive_sell_offer'):
        return 'swap'
    if op_type in CAPITAL_TYPES:
        return 'capital'
    return 'other'


def build_operations(address, since):
    """Every balance-changing operation in the window, with its true net asset flows.

    Effects are the source of the flows and operations are the source of the *intent*
    (which is what separates a swap from a deposit); they are joined on the operation id,
    which is the prefix of every effect's paging_token.
    """
    ops, ops_truncated = _paged_since(f'/accounts/{address}/operations',
                                      {'include_failed': 'true'}, since)
    effects, fx_truncated = _paged_since(f'/accounts/{address}/effects', {}, since)

    by_op = {str(op['id']): op for op in ops}

    flows = collections.defaultdict(lambda: collections.defaultdict(float))
    trade_flows = collections.defaultdict(lambda: collections.defaultdict(float))
    seen_ts = {}
    for e in effects:
        if e.get('account') != address:
            continue
        op_id = str(e['paging_token']).split('-')[0]
        seen_ts.setdefault(op_id, _epoch(e['created_at']))
        kind = e.get('type')
        if kind == 'account_credited':
            flows[op_id][_spec(e)] += float(e['amount'])
        elif kind == 'account_debited':
            flows[op_id][_spec(e)] -= float(e['amount'])
        elif kind == 'trade':
            # sold_/bought_ are from this effect's own account, i.e. ours.
            trade_flows[op_id][_spec(e, 'sold_')] -= float(e['sold_amount'])
            trade_flows[op_id][_spec(e, 'bought_')] += float(e['bought_amount'])
        elif kind == 'liquidity_pool_trade':
            # sold/bought here are the POOL's side of it, so they invert for us.
            sold, bought = e.get('sold', {}), e.get('bought', {})
            trade_flows[op_id][_spec_from_string(sold.get('asset'))] += float(sold.get('amount', 0))
            trade_flows[op_id][_spec_from_string(bought.get('asset'))] -= float(bought.get('amount', 0))

    records = []
    for op_id in set(flows) | set(trade_flows):
        op = by_op.get(op_id, {})
        # Credit/debit effects are preferred wherever they exist: for a multi-hop path
        # payment they already state the net, while the per-hop trade effects describe
        # assets we passed through and never owned. Trade effects are the fallback for
        # an operation that produced none -- a resting offer of ours being taken.
        net = flows.get(op_id) or trade_flows.get(op_id) or {}
        net = {a: v for a, v in net.items() if abs(v) > STROOP / 2}
        if not net:
            continue
        records.append({
            'id': op_id,
            'ts': _epoch(op['created_at']) if op.get('created_at') else seen_ts.get(op_id, since),
            'type': op.get('type', 'unknown'),
            'kind': classify(op, address) if op else 'other',
            'source': op.get('source_account'),
            'from': op.get('from'),
            'to': op.get('to'),
            'flows': net,
            'via_trade_effects': op_id not in flows,
        })
    records.sort(key=lambda r: (r['ts'], r['id']))
    return records, (ops_truncated or fx_truncated)


def fetch_fees(address, since):
    """(fee_xlm, tx_count, failed_count, truncated). Fees are charged for failed
    transactions too -- 11 of them in the reference window -- so include_failed is on;
    excluding them would report a cost the account did not actually avoid."""
    txs, truncated = _paged_since(f'/accounts/{address}/transactions',
                                  {'include_failed': 'true'}, since)
    stroops = 0
    failed = 0
    for t in txs:
        if not t.get('successful'):
            failed += 1
        # A fee-bumped transaction is paid for by someone else; charging it here would
        # invent a cost. fee_account is the field that says who actually paid.
        if t.get('fee_account') == address:
            stroops += int(t.get('fee_charged', 0))
    return stroops * STROOP, len(txs), failed, truncated


def fetch_balances(address):
    account = _get(f'{HORIZON}/accounts/{address}')
    balances = {}
    for b in account['balances']:
        if b.get('asset_type') == 'liquidity_pool_shares':
            continue
        balances[_spec(b)] = float(b['balance'])
    return balances, account


def swap_value_usd(flows, ts, prices):
    """What one swap was worth, in USD, from its own two legs.

    The USDC leg is exact -- it is the settlement asset and the amount is the price. XLM
    is the fallback for a leg pair that never touched USDC. Neither available means the
    swap is unvalued and says so; guessing would put a made-up number into the realized
    column.
    """
    if USDC in flows:
        return abs(flows[USDC]), 'usdc'
    if 'XLM' in flows:
        unit = prices.xlm_at(ts)
        if unit:
            return abs(flows['XLM']) * unit, 'xlm'
    return None, 'unvalued'


class AssetLedger:
    """Avg-cost book for one asset, in USD.

    Seeded with the opening inventory at the baseline mark: this is a *period* P&L, so
    what the position cost before the window began is neither known nor relevant -- the
    baseline mark is the cost of choosing to still hold it at that moment. qty is signed
    so a short (the account owing XLM against the operator's buffer) books the same way,
    with avg_cost the price the short was opened at.
    """

    def __init__(self, qty=0.0, unit_cost=0.0):
        self.qty = qty
        self.avg_cost = unit_cost
        self.realized = 0.0
        self.cash_flow = 0.0
        self.bought_qty = 0.0
        self.sold_qty = 0.0
        self.bought_usd = 0.0
        self.sold_usd = 0.0
        self.n = 0

    def apply(self, qty_delta, usd, *, cash=True, count=True):
        """Book a quantity change worth `usd` (always positive). `cash=False` is a flow
        that changed the position without generating cash -- a deposit, or fee XLM
        leaving -- so it adjusts basis and realizes against it, but must not be counted
        as trading proceeds."""
        if abs(qty_delta) <= STROOP / 2:
            return
        price = usd / abs(qty_delta) if usd is not None else self.avg_cost
        if count:
            # Trading statistics only. A deposit and a fee both move the position and
            # both adjust basis below, but neither is a trade, and counting them here
            # would put the operator's 60 XLM into the volume this account "traded".
            self.n += 1
            if qty_delta > 0:
                self.bought_qty += qty_delta
                self.bought_usd += usd or 0.0
            else:
                self.sold_qty += -qty_delta
                self.sold_usd += usd or 0.0
        if cash:
            self.cash_flow += -(usd or 0.0) if qty_delta > 0 else (usd or 0.0)

        same_direction = self.qty == 0 or (self.qty > 0) == (qty_delta > 0)
        if same_direction:
            new_qty = self.qty + qty_delta
            if abs(new_qty) > STROOP / 2:
                self.avg_cost = ((self.avg_cost * abs(self.qty)) + abs(qty_delta) * price) / abs(new_qty)
            self.qty = new_qty
            return
        closing = min(abs(qty_delta), abs(self.qty))
        sign = 1 if self.qty > 0 else -1
        self.realized += sign * closing * (price - self.avg_cost)
        remainder = abs(qty_delta) - closing
        self.qty += qty_delta
        if remainder > STROOP / 2:
            # Flipped through zero -- the excess opens the other way at this price.
            self.avg_cost = price
        elif abs(self.qty) <= STROOP / 2:
            self.qty = 0.0


def analyze(address, since, prices):
    """The whole reconstruction. Returns a dict the text and JSON views both render."""
    operations, ops_truncated = build_operations(address, since)
    fee_xlm, tx_count, failed_count, tx_truncated = fetch_fees(address, since)
    balances, account = fetch_balances(address)
    now = time.time()

    # Opening balances, reconstructed: what is there now, minus everything that has
    # happened since. Fees are a real XLM outflow that produces no effect of its own, so
    # they have to be added back by hand or the opening balance comes out short.
    deltas = collections.defaultdict(float)
    for op in operations:
        for spec, amount in op['flows'].items():
            deltas[spec] += amount
    deltas['XLM'] -= fee_xlm

    specs = set(balances) | set(deltas)
    opening = {s: balances.get(s, 0.0) - deltas.get(s, 0.0) for s in specs}

    ledgers = {}
    for spec in specs:
        qty = opening[spec]
        unit = 1.0 if spec == USDC else (prices.xlm_at(since) if spec == 'XLM'
                                         else prices.unit_now(spec))
        ledgers[spec] = AssetLedger(qty, unit or 0.0)

    capital_usd = collections.defaultdict(float)
    capital_events = []
    unvalued_swaps = 0

    for op in operations:
        flows = op['flows']
        if op['kind'] == 'swap':
            usd, basis = swap_value_usd(flows, op['ts'], prices)
            if usd is None:
                unvalued_swaps += 1
            op['usd'] = usd
            op['basis'] = basis
            for spec, amount in flows.items():
                ledgers[spec].apply(amount, usd)
        else:
            # Capital, or an operation type this script does not model. Either way the
            # balance really moved, so it is valued at the market of the moment and kept
            # out of P&L rather than dropped -- dropping it would silently reappear as
            # a profit or a loss in the mark-to-market identity.
            total = 0.0
            for spec, amount in flows.items():
                unit = 1.0 if spec == USDC else (prices.xlm_at(op['ts']) if spec == 'XLM'
                                                 else prices.unit_now(spec))
                # An unpriceable flow is valued at zero rather than crashing the report,
                # and the asset lands in `unpriced` below so the output says so. This is
                # the no-/opt/tools path: the ledger half of the report is still true.
                value = amount * (unit or 0.0)
                capital_usd[spec] += value
                total += value
                ledgers[spec].apply(amount, abs(value), cash=False, count=False)
            op['usd'] = total
            op['basis'] = 'capital'
            capital_events.append(op)

    # Fees: XLM that left with nothing coming back. Booked against the ledger at the
    # spot of the moment so the position stays honest, then reported on its own line --
    # it is a cost inside P&L, never capital.
    fee_usd = 0.0
    if fee_xlm:
        fee_usd = fee_xlm * (prices.xlm_at(now) or 0.0)
        ledgers['XLM'].apply(-fee_xlm, fee_usd, cash=False, count=False)

    rows = []
    unpriced = []
    stale_open = []
    value_end_total = value_start_total = 0.0
    for spec in sorted(specs, key=lambda s: (s != 'XLM', s != USDC, s)):
        ledger = ledgers[spec]
        end_qty = balances.get(spec, 0.0)
        start_qty = opening[spec]
        end_value, end_ok = prices.value_now(spec, end_qty)
        start_unit = 1.0 if spec == USDC else (prices.xlm_at(since) if spec == 'XLM'
                                               else prices.unit_now(spec))
        start_ok = start_unit is not None
        start_value = start_qty * (start_unit or 0.0)
        if not end_ok or not start_ok:
            unpriced.append(spec)
        elif spec not in ('XLM', USDC) and abs(start_qty) > STROOP / 2:
            stale_open.append(spec)
        value_end_total += end_value
        value_start_total += start_value

        # The identity. Everything else in this row is a second opinion on it.
        pnl = end_value - start_value - capital_usd.get(spec, 0.0) + ledger.cash_flow
        unit_now = prices.unit_now(spec)
        unrealized = (end_qty * ((unit_now or 0.0) - ledger.avg_cost)) if unit_now is not None else 0.0
        spec_fee = fee_usd if spec == 'XLM' else 0.0
        rows.append({
            'asset': spec,
            'start_qty': start_qty, 'end_qty': end_qty,
            'start_value': start_value, 'end_value': end_value,
            'capital_usd': capital_usd.get(spec, 0.0),
            'cash_flow': ledger.cash_flow,
            'swaps': ledger.n,
            'bought_qty': ledger.bought_qty, 'sold_qty': ledger.sold_qty,
            'avg_buy': (ledger.bought_usd / ledger.bought_qty) if ledger.bought_qty else 0.0,
            'avg_sell': (ledger.sold_usd / ledger.sold_qty) if ledger.sold_qty else 0.0,
            'avg_cost': ledger.avg_cost,
            'realized': ledger.realized,
            'unrealized': unrealized,
            'fee_usd': spec_fee,
            'pnl': pnl,
            'residual': pnl - (ledger.realized + unrealized - spec_fee),
            'priced': end_ok and start_ok,
        })

    capital_net = sum(capital_usd.values())
    return {
        'account': address,
        'since': since,
        'now': now,
        'operations': operations,
        'capital_events': capital_events,
        'rows': rows,
        'unpriced': unpriced,
        'stale_open': stale_open,
        'unvalued_swaps': unvalued_swaps,
        'swap_count': sum(1 for o in operations if o['kind'] == 'swap'),
        'maker_fills': sum(1 for o in operations if o['via_trade_effects']),
        'tx_count': tx_count,
        'failed_tx': failed_count,
        'fee_xlm': fee_xlm,
        'fee_usd': fee_usd,
        'value_start': value_start_total,
        'value_end': value_end_total,
        'capital_net': capital_net,
        'pnl': value_end_total - value_start_total - capital_net,
        'xlm_price_start': prices.xlm_at(since),
        'xlm_price_now': prices.spot_xlm(),
        'balances': balances,
        'truncated': ops_truncated or tx_truncated,
        'notes': prices.notes,
    }


# --- views --------------------------------------------------------------------------

def _short(spec, width=18):
    if spec == 'XLM':
        return 'XLM'
    code, _, issuer = spec.partition(':')
    return f'{code}:{issuer[:4]}'[:width]


def print_report(data, baseline, address_source, from_baseline=True):
    span = data['now'] - data['since']
    print(f"{'ACCOUNT':10} {data['account']}")
    print(f"{'':10} {IDENTITY} via {address_source}")
    print(f"{'WINDOW':10} {_fmt_ts(data['since'])}  ->  {_fmt_ts(data['now'])}"
          f"   ({span / 86400:.1f} days)")
    if from_baseline:
        print(f"{'':10} start is baseline_at from {SHORT_BUFFER_PATH}")
    else:
        print(f"{'':10} start is --since; baseline_at in {SHORT_BUFFER_PATH} overridden")
    print(f"{'LEDGER':10} {data['tx_count']} transactions ({data['failed_tx']} failed), "
          f"{len(data['operations'])} balance-changing operations, "
          f"{data['swap_count']} swaps")
    if data['maker_fills']:
        print(f"{'':10} {data['maker_fills']} of them reconstructed from trade effects "
              f"(resting offers taken, no credit/debit effect)")
    if data['truncated']:
        print(f"{'':10} !! WALK TRUNCATED at {MAX_PAGES} pages -- numbers below are incomplete")
    for note in data['notes']:
        print(f"{'':10} note: {note}")
    print()

    print('CAPITAL FLOWS  (moved in or out without being traded for -- not P&L)')
    if not data['capital_events']:
        print('  none')
    for op in data['capital_events']:
        legs = ', '.join(f'{amount:+.7f} {_short(spec)}'
                         for spec, amount in sorted(op['flows'].items()))
        other = op.get('from') if op.get('to') == data['account'] else op.get('to')
        other = (other or op.get('source') or '?')
        # An operation type this script does not model still moved the balance, so it is
        # valued and held out of P&L like capital -- but it is marked, because treating
        # a trade as a deposit would quietly delete its profit from the headline.
        mark = ' !unmodelled' if op['kind'] == 'other' else ''
        print(f"  {_fmt_ts(op['ts']):19}  {op['type'][:28]:28} {legs:34} "
              f"{op['usd']:+9.4f} USD   {other[:8]}…{mark}")
    print(f"  {'net capital in':19}  {'':28} {'':34} {data['capital_net']:+9.4f} USD")
    if any(op['kind'] == 'other' for op in data['capital_events']):
        print("  !! rows marked !unmodelled are operation types this script has no rule "
              "for (Soroban invokes, pool")
        print("     deposits). They are excluded from P&L as if they were capital, which "
              "is wrong if they were trades.")
    print()

    print('POSITIONS  (opening balance reconstructed from the ledger, not from any log)')
    print(f"  {'asset':18} {'open_qty':>16} {'close_qty':>16} {'open_usd':>10} "
          f"{'close_usd':>10} {'capital':>9} {'cash_flow':>10} {'P&L':>9}")
    for row in data['rows']:
        flag = '' if row['priced'] else '  (unpriced)'
        print(f"  {_short(row['asset']):18} {row['start_qty']:16.7f} {row['end_qty']:16.7f} "
              f"{row['start_value']:10.4f} {row['end_value']:10.4f} "
              f"{row['capital_usd']:+9.4f} {row['cash_flow']:+10.4f} {row['pnl']:+9.4f}{flag}")
    print()

    print('TRADING  (avg-cost view of the same window: did the round trips make money)')
    print(f"  {'asset':14} {'swaps':>6} {'bought_qty':>14} {'avg_buy':>10} "
          f"{'sold_qty':>14} {'avg_sell':>10} {'realized':>10} {'unrealized':>11} "
          f"{'fees':>7} {'resid':>8}")
    for row in data['rows']:
        if not row['swaps'] and not row['fee_usd']:
            continue
        print(f"  {_short(row['asset'], 14):14} {row['swaps']:6d} {row['bought_qty']:14.7f} "
              f"{row['avg_buy']:10.6f} {row['sold_qty']:14.7f} {row['avg_sell']:10.6f} "
              f"{row['realized']:+10.4f} {row['unrealized']:+11.4f} {row['fee_usd']:7.4f} "
              f"{row['residual']:+8.4f}")
    print(f"  ({data['fee_xlm']:.7f} XLM of network fees, charged on failed transactions "
          f"too. avg_buy/avg_sell are the realized rates, so the gap between them is the")
    print(f"   round-trip edge after spread -- for a taker paying it on both sides that is "
          f"the whole game. resid is P&L minus realized+unrealized-fees: the two columns")
    print(f"   are computed independently, and a non-zero resid is a leg whose closing")
    print(f"   value was depth-capped by dex_price.mark_value while its unrealized figure "
          f"was marked at the unit price -- real, and worth knowing, not a rounding slip.)")
    if data['unvalued_swaps']:
        print(f"  !! {data['unvalued_swaps']} swaps had neither a USDC nor an XLM leg and "
              f"could not be valued")
    if data['unpriced']:
        print(f"  !! unpriced legs (excluded from value, so P&L understates them): "
              f"{', '.join(_short(s) for s in data['unpriced'])}")
    if data['stale_open']:
        print(f"  !! opening balance marked at TODAY's price (there is no historical book "
              f"for a discovered asset, only XLM candles): "
              f"{', '.join(_short(s) for s in data['stale_open'])}")
    print()

    xlm_start, xlm_now = data['xlm_price_start'], data['xlm_price_now']
    at_start = f'   (XLM at {xlm_start:.6f})' if xlm_start else '   (XLM unpriced)'
    at_now = f'   (XLM at {xlm_now:.6f})' if xlm_now else '   (XLM unpriced)'
    print('REAL P&L')
    print(f"  {'portfolio at baseline':30} {data['value_start']:+12.4f} USD{at_start}")
    print(f"  {'portfolio now':30} {data['value_end']:+12.4f} USD{at_now}")
    print(f"  {'net capital contributed':30} {data['capital_net']:+12.4f} USD")
    print(f"  {'':30} {'-' * 12}")
    print(f"  {'REAL P&L':30} {data['pnl']:+12.4f} USD")
    invested = data['value_start'] + max(data['capital_net'], 0.0)
    if data['unpriced']:
        # A leg that could not be marked contributes zero to BOTH ends of the identity,
        # so whichever end held it is understated and the difference between them is
        # not a profit or a loss at all. Naming the number NOT MEANINGFUL beats printing
        # it under a warning eight lines further up, and the percentage -- the single
        # most quotable line in the report -- is withheld entirely.
        print(f"  {'':30} {'':>12} !! NOT MEANINGFUL: "
              f"{', '.join(_short(x) for x in data['unpriced'])} could not be marked,")
        print(f"  {'':30} {'':>12}    so the two portfolio values above are not "
              f"comparable. Run this")
        print(f"  {'':30} {'':>12}    in the container, where {TOOLS_DIR} is importable.")
    elif invested > 0:
        print(f"  {'return on capital employed':30} {100 * data['pnl'] / invested:+12.2f} %"
              f"   ({invested:.2f} USD employed over {span / 86400:.1f} days)")
    if data['fee_usd']:
        print(f"  {'  of which network fees':30} {-data['fee_usd']:+12.4f} USD  (memo: "
              f"already inside the number above)")

    _print_buffer_memo(baseline, data)


def _print_buffer_memo(baseline, data):
    """The operator's short-sell collateral, and pubnet_tally's hand-set baseline.

    The buffer arrived as an ordinary payment, so it is already excluded from P&L as
    capital -- but it is still sitting in the closing balance as XLM that is owed back,
    which the closing portfolio value does not say on its own.
    """
    funded = baseline.get('funded_xlm') or 0.0
    if funded:
        unit = data['xlm_price_now'] or 0.0
        print()
        print(f"  {'short buffer outstanding':30} {funded:12.4f} XLM "
              f"({funded * unit:.4f} USD) -- operator collateral inside the closing")
        print(f"  {'':30} balance and owed back; excluded from P&L as capital, "
              f"not from the balance")
    if baseline.get('baseline_xlm') is not None:
        xlm_open = next((r['start_qty'] for r in data['rows'] if r['asset'] == 'XLM'), 0.0)
        print(f"  {'baseline_xlm on file':30} {baseline['baseline_xlm']:12.4f} XLM -- "
              f"pubnet_tally's hand-set offset, not a raw")
        print(f"  {'':30} balance; shown for comparison against the "
              f"{xlm_open:.4f} XLM opening balance")
        print(f"  {'':30} this script reconstructed from Horizon")


def print_trades(data):
    print(f"{'timestamp':19}  {'kind':8} {'type':26} {'legs':44} {'usd':>10} {'src':>9}")
    for op in data['operations']:
        legs = ', '.join(f'{amount:+.7f} {_short(spec)}'
                         for spec, amount in sorted(op['flows'].items()))
        usd = op.get('usd')
        usd_text = f'{usd:10.4f}' if usd is not None else '         -'
        print(f"{_fmt_ts(op['ts']):19}  {op['kind']:8} {op['type'][:26]:26} {legs[:44]:44} "
              f"{usd_text} {op.get('basis', ''):>9}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Real P&L for the claudio pubnet account, from Horizon's ledger.")
    parser.add_argument('--account', help='override the account to report on')
    parser.add_argument('--since', help='window start: epoch or 2026-08-11T20:01:43Z '
                                        '(default: baseline_at in .short_buffer.json)')
    parser.add_argument('--trades', action='store_true',
                        help='also print every balance-changing operation')
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    args = parser.parse_args()

    baseline = read_baseline()
    if args.since:
        since = parse_since(args.since)
    elif baseline['baseline_at']:
        since = float(baseline['baseline_at'])
    else:
        raise SystemExit(
            f'no baseline_at in {SHORT_BUFFER_PATH}'
            + (f' ({baseline["error"]})' if baseline['error'] else '')
            + ' -- pass --since to choose a window explicitly')

    address, address_source = resolve_address(args.account)
    prices = Prices()
    prices.check_window(since)
    data = analyze(address, since, prices)

    if args.json:
        payload = dict(data)
        payload['baseline'] = baseline
        payload['operations'] = [
            {k: v for k, v in op.items() if k != 'flows'} | {'flows': op['flows']}
            for op in data['operations']
        ] if args.trades else len(data['operations'])
        payload.pop('capital_events')
        payload['capital_events'] = [
            {'ts': op['ts'], 'type': op['type'], 'flows': op['flows'], 'usd': op['usd']}
            for op in data['capital_events']
        ]
        print(json.dumps(payload, indent=2, default=str))
        return

    print_report(data, baseline, address_source, from_baseline=not args.since)
    if args.trades:
        print()
        print_trades(data)


if __name__ == '__main__':
    main()

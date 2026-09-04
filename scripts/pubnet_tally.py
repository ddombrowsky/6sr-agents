#!/usr/bin/env python3
"""Running real-money P&L tally for one strategy, from its on-chain fills.

TWO logs, because no single file holds every real fill.

  trades/<name>.pubnet.log -- stellar_trader._log_pubnet_trade's record of what it
      submitted on pubnet. On the TAKER path (domain_sdex) every line is a fill:
      submit_trade writes `buy`/`sell` and wind_down writes `wind_down_sell`. On the
      MAKER path (domain_sdex_maker) NOT ONE LINE IS A FILL -- place_offer and
      cancel_offer write `offer_bid`, `offer_ask` and `offer_cancel`, which are the
      placement, re-price and cancellation of a RESTING offer. An offer is an intention:
      most are cancelled unfilled, several lines describe the same offer (a re-price
      reuses the offer id), and `offer_cancel` carries no notional at all. Counting them
      as fills is how this file came to report a 670 XLM short against a strategy that
      was in fact 4 XLM long.

  trades/<name>.log -- the strategy's own trade log. A maker's fills never reach the
      pubnet log; quote_executor._live_sync detects them by reconciling against Horizon
      and records them here through trade_logger. The real ones are the lines carrying a
      `live` object with `submitted` true. Every other line in this file is paper.

MERGED WITHOUT DOUBLE COUNTING by taking each fill from exactly one side: `buy`, `sell`
and `wind_down_sell` from the pubnet log, `maker_buy` and `maker_sell` from the paper
log. The two sets are disjoint by action name, which matters because the taker path
writes BOTH files for a single fill. Its pubnet line carries the real filled amount
while its paper line carries the *requested* notional (stellar_trader clamps every order
to MAX_TRADE_USD, the remaining daily budget and the real on-chain balance -- see
trade_logger._live_fields), so the pubnet line is the one to believe there. The maker
path writes only the paper line, and there the top-level amount_usd IS the filled
amount, since quote_executor._apply_fill is handed the reconciled fill directly.

Quantities are approximate on the taker leg: amount_xlm there is "estimated from the
pre-trade price, not the exact fill" (stellar_trader.py's own comment), and for a
non-XLM asset it is logged as 0.0, so position, avg cost and unrealized P&L cannot be
derived for those legs from that log alone -- only net USD in vs out. The maker leg
records the reconciled fill's own amount_asset and fill_price and is exact for any
asset.

Deliberately lives outside /opt/tools and /opt/master_agent: those two directories are
what monitor.check_boundary_integrity() watches, and this is a read-only report with no
reason to trip the live-trading halt every time it changes.

Short-selling: a real short-sell draws against /opt/trades/.short_buffer.json, XLM an
operator deposited by hand as collateral (see tools/stellar_trader.py's
SHORT_BUFFER_XLM/_short_buffer_funded) -- not money the strategy earned. _log_pubnet_trade
logs a short-sell exactly like any other sell, so the per-trade ledger below can't tell
them apart; that liability is reported separately rather than guessed at per-trade. See
_read_short_buffer.

  python3 /opt/pubnet_tally.py                  # one summary line per strategy + grand total
  python3 /opt/pubnet_tally.py <name>           # full trade-by-trade running tally for one strategy
  python3 /opt/pubnet_tally.py <name> --fills   # same, but hide the offer lifecycle rows
  python3 /opt/pubnet_tally.py --all            # merged running tally across every strategy, in time order
  python3 /opt/pubnet_tally.py --json           # machine-readable summary
"""
import json
import sys
import time
from collections import Counter
from pathlib import Path

TRADES_DIR = Path('/opt/trades')
SHORT_BUFFER_PATH = TRADES_DIR / '.short_buffer.json'
LIVE_STRATEGY_FILE = Path('/opt/live_strategy.json')

# Which action names move a position, and in which direction.
#
# Signed by an explicit table rather than by `action == 'buy'` with everything else
# falling through to "sell", which is what this file used to do: `offer_bid` is not the
# string 'buy', so every quote a maker ever posted -- both sides of it -- was booked as a
# sell. Classify by exclusion in the safe direction only. An unknown action is counted
# and reported (see _Ledger.unknown_ops), never silently signed, because guessing wrong
# on a real fill puts money in the ledger backwards and guessing wrong on a lifecycle
# line invents a position out of nothing.
_PUBNET_FILLS = {'buy': +1, 'sell': -1, 'wind_down_sell': -1}
_MAKER_FILLS = {'maker_buy': +1, 'maker_sell': -1}
_OFFER_PREFIX = 'offer_'


def _current_live_name():
    """Which strategy currently holds live.flag, per monitor.py's own record.

    Duplicated rather than imported -- same call this file already makes for
    _trade_log_path's fallback: reporting should not drag in the scoring stack.
    baseline_xlm (see _read_short_buffer) is only ever calibrated against whichever
    strategy was live at the moment an operator set it, so the reconstructed real
    balance is only meaningful for that same strategy; showing it against a strategy
    that wasn't live at baseline time would silently mix two unrelated real positions.
    """
    try:
        return json.loads(LIVE_STRATEGY_FILE.read_text())['name']
    except Exception:
        return None


def _read_lines(path):
    entries = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    return entries


def _pubnet_events(path):
    """Normalized events from one *.pubnet.log: taker fills plus offer lifecycle."""
    events = []
    for e in _read_lines(path):
        action = e.get('action') or '?'
        asset = e.get('asset') or 'XLM'
        usd = float(e.get('amount_usd') or 0.0)
        amount = float(e.get('amount_xlm') or 0.0)
        sign = _PUBNET_FILLS.get(action)
        if sign is None:
            # An offer's own size and price are still worth showing -- that is the
            # quote the strategy actually posted -- they just never reach a ledger.
            kind = 'offer' if action.startswith(_OFFER_PREFIX) else 'unknown'
            events.append({'ts': e.get('timestamp') or 0, 'kind': kind, 'action': action,
                           'asset': asset, 'usd': usd, 'qty': amount, 'side': 0,
                           'price': (usd / amount) if amount else None,
                           'source': 'pubnet'})
            continue
        # amount_xlm is logged as 0.0 for every non-XLM leg, so those contribute USD
        # flow but no derivable position -- unchanged from this file's original
        # behaviour, and the reason non-XLM legs are reported as their own ledger.
        qty = sign * amount if asset == 'XLM' else 0.0
        events.append({'ts': e.get('timestamp') or 0, 'kind': 'fill', 'action': action,
                       'asset': asset, 'usd': usd, 'qty': qty, 'side': sign,
                       'price': None, 'source': 'pubnet'})
    return events


def _maker_events(path):
    """Reconciled maker fills from one paper trade log -- the live-submitted lines only.

    A line without a truthy `live.submitted` is a paper fill from the strategy's paper
    life (or a live submission that was refused) and is not real money. `amount_asset`
    is already signed by quote_executor._apply_fill and is the reconciled fill, so
    nothing here has to infer a direction from the action name; the action is still
    checked against _MAKER_FILLS so an unrecognised one is reported rather than trusted.
    """
    events = []
    for e in _read_lines(path):
        live = e.get('live')
        if not (isinstance(live, dict) and live.get('submitted')):
            continue
        action = e.get('action') or '?'
        asset = e.get('asset_spec') or e.get('asset') or 'XLM'
        usd = float(e.get('amount_usd') or 0.0)
        sign = _MAKER_FILLS.get(action)
        if sign is None:
            # A live-submitted `buy`/`sell` is the TAKER path, whose fill is already
            # coming from the pubnet log with the real (not requested) amount; anything
            # else is genuinely unrecognised. Neither may be booked here.
            if action not in _PUBNET_FILLS:
                events.append({'ts': e.get('timestamp') or 0, 'kind': 'unknown',
                               'action': action, 'asset': asset, 'usd': usd, 'qty': 0.0,
                               'side': 0, 'price': None, 'source': 'log'})
            continue
        qty = e.get('amount_asset')
        qty = float(qty) if qty is not None else float(e.get('amount_xlm') or 0.0)
        price = e.get('fill_price')
        events.append({'ts': e.get('timestamp') or 0, 'kind': 'fill', 'action': action,
                       'asset': asset, 'usd': usd, 'qty': qty, 'side': sign,
                       'price': float(price) if price else None, 'source': 'log'})
    return events


def strategy_events(name):
    """Every real-money event for one strategy, both logs merged, in time order."""
    events = _pubnet_events(TRADES_DIR / f'{name}.pubnet.log')
    events += _maker_events(TRADES_DIR / f'{name}.log')
    events.sort(key=lambda e: e['ts'])
    return events


class _Ledger:
    """Avg-cost running position for one (strategy, asset) pair.

    Fed signed fills only. self.qty is signed: positive is an ordinary long position,
    negative is a short (XLM owed back, e.g. against the buffer in .short_buffer.json).
    avg_cost is always a positive per-unit price -- the cost basis of a long, or the
    price a short was opened at -- either way, what closing at the current price nets
    against.

    Offer lifecycle and unrecognised actions are counted but never applied: an offer is
    an order that may or may not ever trade, and booking one as a fill is a position the
    account does not hold.
    """

    def __init__(self):
        self.qty = 0.0
        self.avg_cost = 0.0
        self.realized_pl = 0.0
        self.cash_flow = 0.0  # received - spent
        self.last_price = None
        self.n = 0                    # fills
        self.bought_qty = 0.0         # gross, for the pre-log inventory check below
        self.wind_down_qty = 0.0
        self.pre_log_qty = 0.0        # wound-down inventory never recorded as bought
        self.offer_ops = Counter()    # offer_bid / offer_ask / offer_cancel
        self.unknown_ops = Counter()

    def note(self, event):
        """Record a non-fill event. Position, cost basis and P&L are untouched."""
        if event['kind'] == 'offer':
            self.offer_ops[event['action']] += 1
        else:
            self.unknown_ops[event['action']] += 1

    def apply(self, usd, qty, price=None, side=0, action=None):
        """Book one fill. `qty` is signed: positive bought, negative sold.

        `side` (+1 bought, -1 sold) is carried separately because a non-XLM taker leg
        has a known USD flow and an unknown quantity -- amount_xlm is logged as 0.0 for
        those -- and the direction of the money is the one thing still recoverable.
        """
        self.n += 1
        if price is None and qty:
            price = usd / abs(qty)
        if price:
            self.last_price = price
        side = side or (1 if qty > 0 else -1 if qty < 0 else 0)
        self.cash_flow += (-usd if side > 0 else usd) if side else 0.0
        if qty > 0:
            self.bought_qty += qty
        if action == 'wind_down_sell':
            self.wind_down_qty += abs(qty)
            # A wind_down liquidates inventory. Whatever it sold beyond what the ledger
            # was actually holding at that moment is inventory the account already had
            # when logging began -- the one case where the reconstructed short is an
            # artifact rather than a real position. Measured here, at the moment of the
            # sale, because a later round trip can carry the position back below zero
            # honestly and must not be blamed on the wind_down.
            self.pre_log_qty += max(0.0, abs(qty) - max(0.0, self.qty))
        if not qty:
            # A non-XLM taker leg: the USD moved and is booked above, but this log
            # cannot say how much of the asset it bought, so there is no position,
            # cost basis or P&L to derive.
            return

        same_direction = self.qty == 0 or (self.qty > 0) == (qty > 0)
        if same_direction:
            # Opening, or adding to, a position in this direction -- long+buy or
            # short+sell alike.
            new_qty = self.qty + qty
            self.avg_cost = ((self.avg_cost * abs(self.qty)) + usd) / abs(new_qty)
            self.qty = new_qty
        else:
            # Reducing or closing -- selling a long, or buying back (covering) a short.
            closing = min(abs(qty), abs(self.qty))
            sign = 1 if self.qty > 0 else -1
            # A zero notional against a non-zero qty gives price 0.0, which would book a
            # fabricated loss the full size of the basis. Nothing is knowable about the
            # P&L of a fill with no price, so close it flat.
            self.realized_pl += sign * closing * ((price or self.avg_cost) - self.avg_cost)
            remainder = abs(qty) - closing
            self.qty += qty
            if remainder > 1e-12:
                # Flipped through zero: the excess opens a new position at this trade's
                # price (e.g. covering a short and going long in the same fill).
                self.avg_cost = price
            elif abs(self.qty) < 1e-12:
                self.qty = 0.0
                self.avg_cost = 0.0

    @property
    def unrealized_pl(self):
        if self.last_price is None:
            return 0.0
        return self.qty * (self.last_price - self.avg_cost)

    @property
    def total_pl(self):
        return self.realized_pl + self.unrealized_pl


def tally(events):
    """{asset: _Ledger} for one strategy's events, processed in order."""
    ledgers = {}
    for e in events:
        ledger = ledgers.setdefault(e['asset'], _Ledger())
        if e['kind'] == 'fill':
            ledger.apply(e['usd'], e['qty'], e['price'], e['side'], e['action'])
        else:
            ledger.note(e)
    return ledgers


def _fmt_ts(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else '?'


def _strategy_name(path):
    return path.name[:-len('.pubnet.log')]


def _strategy_names():
    """Every strategy with either log present, so a maker that has only ever rested
    offers (no pubnet fills) and one that has only paper-era lines both show up."""
    names = {p.name[:-len('.pubnet.log')] for p in TRADES_DIR.glob('*.pubnet.log')}
    for p in TRADES_DIR.glob('*.log'):
        if p.name.endswith('.pubnet.log') or p.name.endswith('.run.log'):
            continue
        names.add(p.name[:-len('.log')])
    return sorted(names)


def _phantom_short_note(ledgers):
    """Warn when the reconstructed position is short because the log never saw the buy.

    These logs start when a strategy starts; whatever XLM the account already held is
    invisible to them. A `wind_down_sell` liquidates exactly that kind of inventory, so
    a strategy that wound down more than it was ever recorded buying reads back as a
    large short it does not hold -- seed_maker04 shows -251 XLM against 11 wind-down
    sells and no buys at all. The number is right about the FLOWS and wrong about the POSITION, which
    makes avg_cost and unrealized P&L on that leg meaningless too. Said out loud rather
    than papered over: reconciling it needs baseline_xlm (see _read_short_buffer), which
    is a hand-set real-account fact this file cannot derive.
    """
    flagged = []
    for asset, ledger in ledgers.items():
        if ledger.pre_log_qty > 1e-9:
            flagged.append(
                f'{asset} pos {ledger.qty:+.4f} includes {ledger.pre_log_qty:.4f} '
                f'wound down that was never recorded bought')
    if not flagged:
        return ''
    return ('PRE-LOG INVENTORY: ' + '; '.join(flagged)
            + ' -- the short is an artifact of the log starting after the position did; '
              'flows are real, pos/avg_cost/unrealized on that leg are not')


def _ops_note(ledgers):
    """One-line description of what did NOT move the position, or '' if nothing."""
    offers, unknown = Counter(), Counter()
    for ledger in ledgers.values():
        offers.update(ledger.offer_ops)
        unknown.update(ledger.unknown_ops)
    parts = []
    if offers:
        parts.append('offers not counted as fills: '
                     + ', '.join(f'{a} {n}' for a, n in sorted(offers.items())))
    if unknown:
        parts.append('UNRECOGNISED actions, not counted: '
                     + ', '.join(f'{a} {n}' for a, n in sorted(unknown.items())))
    return '; '.join(parts)


def _read_short_buffer():
    """Operator-funded short-sell collateral (tools/stellar_trader.py's SHORT_BUFFER_XLM),
    written once by hand after a real deposit -- never by code, never per-strategy. An
    absent or unreadable file means unfunded, 0.0, matching
    stellar_trader._short_buffer_funded's own default rather than raising.

    `baseline_xlm` is a second, independent hand-set fact recorded in the same file
    (reused rather than adding a new marker file, since this is the one place an
    operator already writes a verified real-account number by hand): the gap between
    what this tally can reconstruct from the trade logs alone -- which only sees fills
    logged since each strategy started, not whatever XLM the account already held or
    any manual deposit/withdrawal -- and the real on-chain balance, net of the short
    buffer above. `real_balance - short_buffer - trade_log_qty` at the moment it's set.
    None (not 0.0) when never set, so callers can tell "known to be zero" apart from
    "never reconciled" and skip the display line entirely in the latter case.

    A baseline set before offers stopped being booked as fills is calibrated against a
    net qty that was wrong by hundreds of XLM. Re-set it, don't carry it over.
    """
    try:
        with SHORT_BUFFER_PATH.open() as f:
            record = json.load(f)
        baseline_xlm = record.get('baseline_xlm')
        return {'funded_xlm': float(record.get('funded_xlm', 0.0)),
                'funded_at': record.get('funded_at'),
                'baseline_xlm': float(baseline_xlm) if baseline_xlm is not None else None,
                'baseline_at': record.get('baseline_at')}
    except Exception:
        return {'funded_xlm': 0.0, 'funded_at': None, 'baseline_xlm': None, 'baseline_at': None}


def _current_xlm_price(fallback=None):
    """Live XLM/USD via tools/price_feed.py if reachable, else `fallback` (typically the
    most recent trade price seen in the logs). Best-effort only -- this is a read-only
    report outside /opt/tools and must not fail if that module can't be imported."""
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import price_feed
        price = price_feed.get_price()
        if price:
            return price
    except Exception:
        pass
    return fallback


def _print_buffer_footer(fallback_price=None, baseline_total=None, baseline_label='TOTAL',
                          total_xlm_qty=None):
    """Shared text-mode footer for print_verbose/print_summary/print_merged so the short
    buffer liability shows up regardless of which view was asked for. No-op if the buffer
    has never been funded, so an unfunded system's output is unchanged.

    `total_xlm_qty` is the caller's own net XLM qty as reconstructed from the fills in
    both logs (the same number the view above it already showed). Added to baseline_xlm
    (see _read_short_buffer) it reconstructs an estimate of the real on-chain XLM
    balance, net of the short buffer -- printed only when both a baseline has been set
    and the caller has a qty to add it to.
    """
    buffer = _read_short_buffer()
    if buffer['baseline_xlm'] is not None and total_xlm_qty is not None:
        real_balance = buffer['baseline_xlm'] + total_xlm_qty
        print(f"{'REAL XLM BALANCE (est.)':24} {real_balance:12.4f} XLM  "
              f"(baseline {buffer['baseline_xlm']:.4f} + trade-log net {total_xlm_qty:+.4f}; "
              f"net of short buffer; baseline hand-set, see .short_buffer.json)")
    if not buffer['funded_xlm']:
        return
    xlm_price = _current_xlm_price(fallback=fallback_price)
    buffer_usd = buffer['funded_xlm'] * xlm_price if xlm_price else None
    funded_at = _fmt_ts(buffer['funded_at']) if buffer['funded_at'] else '?'
    usd_str = f"{buffer_usd:+8.4f} USD" if buffer_usd is not None else "USD (no XLM price available)"
    print(f"\n{'SHORT BUFFER':24} funded {buffer['funded_xlm']:.4f} XLM at {funded_at}  "
          f"liability {usd_str}")
    print("  (operator-deposited short-sell collateral, owed back -- not strategy "
          "P&L, not included in the total above)")
    if buffer_usd is not None and baseline_total is not None:
        print(f"{baseline_label:24} {'':4}{'':17}{'':22}total "
              f"{baseline_total - buffer_usd:+8.4f} USD  (net of short buffer)")


def print_verbose(name, events, show_offers=True):
    # baseline_xlm is only calibrated against whichever strategy was live when an
    # operator set it -- applying it to any other strategy's qty would mix in a real
    # position baseline knows nothing about. Only the XLM leg's "pos" gets the offset;
    # baseline is an XLM-only real-account reconciliation, and a non-XLM leg's qty is
    # never part of what it was solved against.
    is_live = name == _current_live_name()
    baseline_xlm = _read_short_buffer()['baseline_xlm'] if is_live else None
    pos_header = 'real_pos' if baseline_xlm is not None else 'pos'

    ledgers = {}
    print(f"{'timestamp':19}  {'asset':10} {'action':14} {'usd':>9} {'qty':>12} "
          f"{pos_header:>12} {'avg_cost':>10} {'realized':>10} {'unrealized':>11} {'total':>10}")
    if baseline_xlm is not None:
        print(f"  (real_pos = baseline {baseline_xlm:.4f} + trade-log net; negative means "
              f"genuinely short, not just net-sold since the log started)")
    for e in events:
        ledger = ledgers.setdefault(e['asset'], _Ledger())
        fill = e['kind'] == 'fill'
        if fill:
            ledger.apply(e['usd'], e['qty'], e['price'], e['side'], e['action'])
        else:
            ledger.note(e)
            if not show_offers:
                continue
        pos = (ledger.qty + baseline_xlm
               if (e['asset'] == 'XLM' and baseline_xlm is not None) else ledger.qty)
        # An offer row shows the quote's own size in the usd/qty columns, but the
        # position columns are the ledger's -- unchanged, because nothing traded.
        print(f"{_fmt_ts(e['ts']):19}  {e['asset'][:10]:10} {e['action']:14} "
              f"{e['usd']:9.4f} {e['qty']:12.4f} {pos:12.4f} {ledger.avg_cost:10.6f} "
              f"{ledger.realized_pl:10.4f} {ledger.unrealized_pl:11.4f} {ledger.total_pl:10.4f}"
              f"{'' if fill else '   (offer, no fill)' if e['kind'] == 'offer' else '   (?)'}")
    print()
    for asset, ledger in ledgers.items():
        print(f"{name} / {asset}: {ledger.n} fills, cash flow {ledger.cash_flow:+.4f} USD, "
              f"pos {ledger.qty:+.4f} @ {ledger.avg_cost:.6f}, "
              f"realized {ledger.realized_pl:+.4f}, unrealized {ledger.unrealized_pl:+.4f}, "
              f"total P&L {ledger.total_pl:+.4f} USD")
    note = _ops_note(ledgers)
    if note:
        print(f"  ({note})")
    phantom = _phantom_short_note(ledgers)
    if phantom:
        print(f"  {phantom}")

    xlm_ledger = ledgers.get('XLM')
    _print_buffer_footer(
        fallback_price=xlm_ledger.last_price if xlm_ledger else None,
        baseline_total=sum(l.total_pl for l in ledgers.values()),
        baseline_label='TOTAL',
        total_xlm_qty=(xlm_ledger.qty if xlm_ledger else 0.0) if is_live else None)


def print_summary(names, as_json=False):
    rows = []
    grand_total = 0.0
    fallback_price, fallback_ts = None, -1
    for name in names:
        events = strategy_events(name)
        if not events:
            continue
        ledgers = tally(events)
        fills = [e for e in events if e['kind'] == 'fill']
        first_ts = fills[0]['ts'] if fills else events[0]['ts']
        last_ts = fills[-1]['ts'] if fills else events[-1]['ts']
        strat_total = sum(l.total_pl for l in ledgers.values())
        grand_total += strat_total
        rows.append({
            'name': name, 'fills': len(fills), 'events': len(events),
            'first': _fmt_ts(first_ts), 'last': _fmt_ts(last_ts),
            'assets': {a: {'fills': l.n, 'qty': round(l.qty, 6),
                           'avg_cost': round(l.avg_cost, 8),
                           'cash_flow': round(l.cash_flow, 4),
                           'realized_pl': round(l.realized_pl, 4),
                           'unrealized_pl': round(l.unrealized_pl, 4),
                           'total_pl': round(l.total_pl, 4),
                           'offer_ops': dict(l.offer_ops),
                           'unknown_ops': dict(l.unknown_ops),
                           # qty/avg_cost/unrealized_pl on this leg are unreliable by
                           # exactly this much -- see _phantom_short_note.
                           'pre_log_qty': round(l.pre_log_qty, 6)}
                       for a, l in ledgers.items()},
            'total_pl': round(strat_total, 4),
        })
        xlm_ledger = ledgers.get('XLM')
        if xlm_ledger and xlm_ledger.last_price and (last_ts or 0) > fallback_ts:
            fallback_ts, fallback_price = (last_ts or 0), xlm_ledger.last_price
    rows.sort(key=lambda r: r['first'])

    buffer = _read_short_buffer()
    xlm_price = _current_xlm_price(fallback=fallback_price)
    buffer_usd = buffer['funded_xlm'] * xlm_price if (buffer['funded_xlm'] and xlm_price) else None
    net_of_buffer = grand_total - buffer_usd if buffer_usd is not None else None

    # baseline_xlm is only calibrated against whichever strategy was live at the time an
    # operator set it -- so the reconstructed real balance only means anything for that
    # same (current) live strategy's own qty, never a sum across strategies that were
    # never covered by that calibration.
    live_name = _current_live_name()
    live_row = next((r for r in rows if r['name'] == live_name), None)
    live_xlm_qty = live_row['assets'].get('XLM', {}).get('qty', 0.0) if live_row else None
    real_balance = (buffer['baseline_xlm'] + live_xlm_qty
                    if buffer['baseline_xlm'] is not None and live_xlm_qty is not None else None)

    if as_json:
        print(json.dumps({
            'strategies': rows,
            'grand_total_pl': round(grand_total, 4),
            'short_buffer': {
                'funded_xlm': buffer['funded_xlm'],
                'funded_at': buffer['funded_at'],
                'xlm_price': xlm_price,
                'liability_usd': round(buffer_usd, 4) if buffer_usd is not None else None,
                'note': 'operator-funded short-sell collateral owed back; not strategy P&L',
            },
            'grand_total_pl_net_of_short_buffer': (
                round(net_of_buffer, 4) if net_of_buffer is not None else None),
            'real_xlm_balance': {
                'live_strategy': live_name,
                'baseline_xlm': buffer['baseline_xlm'],
                'baseline_at': buffer['baseline_at'],
                'live_strategy_trade_log_xlm': (
                    round(live_xlm_qty, 6) if live_xlm_qty is not None else None),
                'estimated_balance_xlm': round(real_balance, 6) if real_balance is not None else None,
                'note': ('baseline_xlm + live strategy\'s own trade-log net; net of short '
                         'buffer; baseline is hand-set and only valid for the strategy live '
                         'when it was set'),
            },
        }, indent=2))
        return

    for r in rows:
        assets = ', '.join(f"{a}: {v['total_pl']:+.4f}" for a, v in r['assets'].items())
        offers = sum(sum(v['offer_ops'].values()) for v in r['assets'].values())
        offer_str = f"  +{offers} offer ops" if offers else ''
        print(f"{r['name']:24} {r['fills']:4} fills  {r['first']} -> {r['last']}  "
              f"total {r['total_pl']:+8.4f} USD  ({assets}){offer_str}")
    print(f"\n{'GRAND TOTAL':24} {'':4}{'':17}{'':22}total {grand_total:+8.4f} USD "
          f"across {len(rows)} strateg{'y' if len(rows) == 1 else 'ies'}")

    _print_buffer_footer(fallback_price=fallback_price, baseline_total=grand_total,
                          baseline_label='GRAND TOTAL', total_xlm_qty=live_xlm_qty)


def print_merged(names, show_offers=False):
    """One running tally across every strategy's fills, in real chronological order.

    Offer lifecycle is hidden by default here: across a whole population it is thousands
    of rows that move no number in the table. `--offers` shows them.
    """
    tagged = []
    for name in names:
        for e in strategy_events(name):
            tagged.append((e['ts'], name, e))
    tagged.sort(key=lambda t: t[0])

    ledgers = {}  # (strategy, asset) -> _Ledger
    running_total = 0.0
    fallback_price = None
    print(f"{'timestamp':19}  {'strategy':24} {'asset':10} {'action':14} {'usd':>9} "
          f"{'strat_total':>11} {'running_total':>13}")
    for ts, name, e in tagged:
        ledger = ledgers.setdefault((name, e['asset']), _Ledger())
        if e['kind'] != 'fill':
            ledger.note(e)
            if not show_offers:
                continue
        else:
            prev = ledger.total_pl
            ledger.apply(e['usd'], e['qty'], e['price'], e['side'], e['action'])
            running_total += ledger.total_pl - prev
            if e['asset'] == 'XLM' and ledger.last_price:
                fallback_price = ledger.last_price
        print(f"{_fmt_ts(ts):19}  {name[:24]:24} {e['asset'][:10]:10} {e['action']:14} "
              f"{e['usd']:9.4f} {ledger.total_pl:11.4f} {running_total:13.4f}")
    print(f"\nrunning total across all strategies/legs: {running_total:+.4f} USD")
    note = _ops_note(ledgers)
    if note:
        print(f"  ({note})")

    live_name = _current_live_name()
    live_xlm_qty = ledgers[(live_name, 'XLM')].qty if (live_name, 'XLM') in ledgers else None
    _print_buffer_footer(fallback_price=fallback_price, baseline_total=running_total,
                          baseline_label='RUNNING TOTAL', total_xlm_qty=live_xlm_qty)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    as_json = '--json' in sys.argv
    merged = '--all' in sys.argv
    fills_only = '--fills' in sys.argv
    show_offers = '--offers' in sys.argv

    names = _strategy_names()
    if not names:
        print(f'no trade logs in {TRADES_DIR}')
        return

    if args:
        if args[0] not in names:
            print(f'no logs for {args[0]} in {TRADES_DIR}')
            return
        print_verbose(args[0], strategy_events(args[0]), show_offers=not fills_only)
    elif merged:
        print_merged(names, show_offers=show_offers)
    else:
        print_summary(names, as_json=as_json)


if __name__ == '__main__':
    main()

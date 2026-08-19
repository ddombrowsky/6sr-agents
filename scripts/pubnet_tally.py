#!/usr/bin/env python3
"""Running real-money P&L tally from trades/*.pubnet.log.

Each *.pubnet.log is stellar_trader._log_pubnet_trade's own record of what it actually
submitted on pubnet for one strategy (buy/sell/wind_down_sell, amount_usd, amount_xlm,
tx_hash, asset). Unlike the paper .log, this is real fills only -- but note amount_xlm is
"estimated from the pre-trade price, not the exact fill" (stellar_trader.py's own comment),
so P&L here is an approximation, not a ledger reconciliation.

Only the XLM leg carries a usable quantity: for a non-XLM asset (spec != 'XLM'),
amount_xlm is always logged as 0.0, so position/avg-cost/unrealized P&L cannot be derived
for those legs from this log alone -- only net USD in vs out. Reported separately per
asset rather than silently folded into the XLM numbers.

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
  python3 /opt/pubnet_tally.py --all            # merged running tally across every strategy, in time order
  python3 /opt/pubnet_tally.py --json           # machine-readable summary
"""
import json
import sys
import time
from pathlib import Path

TRADES_DIR = Path('/opt/trades')
SHORT_BUFFER_PATH = TRADES_DIR / '.short_buffer.json'
LIVE_STRATEGY_FILE = Path('/opt/live_strategy.json')


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


def _read_entries(path):
    entries = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            entries.append(e)
    entries.sort(key=lambda e: e.get('timestamp') or 0)
    return entries


class _Ledger:
    """Avg-cost running position for one (strategy, asset) pair.

    self.qty is signed: positive is an ordinary long position, negative is a short (XLM
    owed back, e.g. against the buffer in .short_buffer.json). avg_cost is always a
    positive per-unit price -- the cost basis of a long, or the price a short was opened
    at -- either way, what closing at the current price nets against.
    """

    def __init__(self):
        self.qty = 0.0
        self.avg_cost = 0.0
        self.realized_pl = 0.0
        self.cash_flow = 0.0  # received - spent
        self.last_price = None
        self.n = 0

    def apply(self, action, usd, qty):
        self.n += 1
        price = (usd / qty) if qty else None
        if price is not None:
            self.last_price = price
        if not qty:
            return

        delta = qty if action == 'buy' else -qty
        self.cash_flow += -usd if action == 'buy' else usd

        same_direction = self.qty == 0 or (self.qty > 0) == (delta > 0)
        if same_direction:
            # Opening, or adding to, a position in this direction -- long+buy or
            # short+sell alike.
            new_qty = self.qty + delta
            self.avg_cost = ((self.avg_cost * abs(self.qty)) + usd) / abs(new_qty)
            self.qty = new_qty
        else:
            # Reducing or closing -- selling a long, or buying back (covering) a short.
            # A sell while short (self.qty < 0, delta < 0) can't reach here since that's
            # same_direction; this branch only sees a genuine reduction.
            closing = min(abs(delta), abs(self.qty))
            sign = 1 if self.qty > 0 else -1
            self.realized_pl += sign * closing * (price - self.avg_cost)
            remainder = abs(delta) - closing
            self.qty += delta
            if remainder > 1e-12:
                # Flipped through zero: the excess opens a new position at this trade's
                # price (e.g. covering a short and going long in the same fill).
                self.avg_cost = price
            elif self.qty == 0:
                self.avg_cost = 0.0

    @property
    def unrealized_pl(self):
        if self.last_price is None:
            return 0.0
        return self.qty * (self.last_price - self.avg_cost)

    @property
    def total_pl(self):
        return self.realized_pl + self.unrealized_pl


def tally(entries):
    """{asset: _Ledger} for one strategy's entries, processed in order."""
    ledgers = {}
    for e in entries:
        asset = e.get('asset') or 'XLM'
        ledger = ledgers.setdefault(asset, _Ledger())
        usd = float(e.get('amount_usd') or 0.0)
        qty = float(e.get('amount_xlm') or 0.0) if asset == 'XLM' else 0.0
        ledger.apply(e.get('action'), usd, qty)
    return ledgers


def _fmt_ts(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else '?'


def _strategy_name(path):
    return path.name[:-len('.pubnet.log')]


def _read_short_buffer():
    """Operator-funded short-sell collateral (tools/stellar_trader.py's SHORT_BUFFER_XLM),
    written once by hand after a real deposit -- never by code, never per-strategy. An
    absent or unreadable file means unfunded, 0.0, matching
    stellar_trader._short_buffer_funded's own default rather than raising.

    `baseline_xlm` is a second, independent hand-set fact recorded in the same file
    (reused rather than adding a new marker file, since this is the one place an
    operator already writes a verified real-account number by hand): the gap between
    what this tally can reconstruct from *.pubnet.log alone -- which only sees trades
    logged since each strategy started, not whatever XLM the account already held or
    any manual deposit/withdrawal -- and the real on-chain balance, net of the short
    buffer above. `real_balance - short_buffer - trade_log_qty` at the moment it's set.
    None (not 0.0) when never set, so callers can tell "known to be zero" apart from
    "never reconciled" and skip the display line entirely in the latter case.
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

    `total_xlm_qty` is the caller's own net XLM qty as reconstructed purely from
    *.pubnet.log (the same number the view above it already showed). Added to
    baseline_xlm (see _read_short_buffer) it reconstructs an estimate of the real
    on-chain XLM balance, net of the short buffer -- printed only when both a baseline
    has been set and the caller has a qty to add it to.
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


def print_verbose(name, entries):
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
    for e in entries:
        asset = e.get('asset') or 'XLM'
        ledger = ledgers.setdefault(asset, _Ledger())
        usd = float(e.get('amount_usd') or 0.0)
        qty = float(e.get('amount_xlm') or 0.0) if asset == 'XLM' else 0.0
        ledger.apply(e.get('action'), usd, qty)
        pos = ledger.qty + baseline_xlm if (asset == 'XLM' and baseline_xlm is not None) else ledger.qty
        print(f"{_fmt_ts(e.get('timestamp')):19}  {asset[:10]:10} {e.get('action', '?'):14} "
              f"{usd:9.4f} {qty:12.4f} {pos:12.4f} {ledger.avg_cost:10.6f} "
              f"{ledger.realized_pl:10.4f} {ledger.unrealized_pl:11.4f} {ledger.total_pl:10.4f}")
    print()
    for asset, ledger in ledgers.items():
        print(f"{name} / {asset}: {ledger.n} trades, cash flow {ledger.cash_flow:+.4f} USD, "
              f"realized {ledger.realized_pl:+.4f}, unrealized {ledger.unrealized_pl:+.4f}, "
              f"total P&L {ledger.total_pl:+.4f} USD")

    xlm_ledger = ledgers.get('XLM')
    _print_buffer_footer(
        fallback_price=xlm_ledger.last_price if xlm_ledger else None,
        baseline_total=sum(l.total_pl for l in ledgers.values()),
        baseline_label='TOTAL',
        total_xlm_qty=(xlm_ledger.qty if xlm_ledger else 0.0) if is_live else None)


def print_summary(files, as_json=False):
    rows = []
    grand_total = 0.0
    fallback_price, fallback_ts = None, -1
    for path in files:
        entries = _read_entries(path)
        if not entries:
            continue
        name = _strategy_name(path)
        ledgers = tally(entries)
        first_ts, last_ts = entries[0].get('timestamp'), entries[-1].get('timestamp')
        strat_total = sum(l.total_pl for l in ledgers.values())
        grand_total += strat_total
        rows.append({
            'name': name, 'trades': len(entries),
            'first': _fmt_ts(first_ts), 'last': _fmt_ts(last_ts),
            'assets': {a: {'trades': l.n, 'qty': round(l.qty, 6),
                           'cash_flow': round(l.cash_flow, 4),
                           'realized_pl': round(l.realized_pl, 4),
                           'unrealized_pl': round(l.unrealized_pl, 4),
                           'total_pl': round(l.total_pl, 4)} for a, l in ledgers.items()},
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
        print(f"{r['name']:24} {r['trades']:4} trades  {r['first']} -> {r['last']}  "
              f"total {r['total_pl']:+8.4f} USD  ({assets})")
    print(f"\n{'GRAND TOTAL':24} {'':4}{'':17}{'':22}total {grand_total:+8.4f} USD "
          f"across {len(rows)} strateg{'y' if len(rows) == 1 else 'ies'}")

    _print_buffer_footer(fallback_price=fallback_price, baseline_total=grand_total,
                          baseline_label='GRAND TOTAL', total_xlm_qty=live_xlm_qty)


def print_merged(files):
    """One running tally across every strategy's fills, in real chronological order."""
    tagged = []
    for path in files:
        name = _strategy_name(path)
        for e in _read_entries(path):
            tagged.append((e.get('timestamp') or 0, name, e))
    tagged.sort(key=lambda t: t[0])

    ledgers = {}  # (strategy, asset) -> _Ledger
    running_total = 0.0
    fallback_price = None
    print(f"{'timestamp':19}  {'strategy':24} {'asset':10} {'action':14} {'usd':>9} "
          f"{'strat_total':>11} {'running_total':>13}")
    for ts, name, e in tagged:
        asset = e.get('asset') or 'XLM'
        ledger = ledgers.setdefault((name, asset), _Ledger())
        usd = float(e.get('amount_usd') or 0.0)
        qty = float(e.get('amount_xlm') or 0.0) if asset == 'XLM' else 0.0
        prev = ledger.total_pl
        ledger.apply(e.get('action'), usd, qty)
        running_total += ledger.total_pl - prev
        if asset == 'XLM' and ledger.last_price:
            fallback_price = ledger.last_price
        print(f"{_fmt_ts(ts):19}  {name[:24]:24} {asset[:10]:10} {e.get('action', '?'):14} "
              f"{usd:9.4f} {ledger.total_pl:11.4f} {running_total:13.4f}")
    print(f"\nrunning total across all strategies/legs: {running_total:+.4f} USD")

    live_name = _current_live_name()
    live_xlm_qty = ledgers[(live_name, 'XLM')].qty if (live_name, 'XLM') in ledgers else None
    _print_buffer_footer(fallback_price=fallback_price, baseline_total=running_total,
                          baseline_label='RUNNING TOTAL', total_xlm_qty=live_xlm_qty)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    as_json = '--json' in sys.argv
    merged = '--all' in sys.argv

    files = sorted(TRADES_DIR.glob('*.pubnet.log'))
    if not files:
        print(f'no *.pubnet.log files in {TRADES_DIR}')
        return

    if args:
        path = TRADES_DIR / f'{args[0]}.pubnet.log'
        if not path.exists():
            print(f'no such log: {path}')
            return
        print_verbose(args[0], _read_entries(path))
    elif merged:
        print_merged(files)
    else:
        print_summary(files, as_json=as_json)


if __name__ == '__main__':
    main()

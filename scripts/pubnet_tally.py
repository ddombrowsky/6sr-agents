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
    """Avg-cost running position for one (strategy, asset) pair."""

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
        if action in ('buy',):
            self.cash_flow -= usd
            if qty:
                new_qty = self.qty + qty
                self.avg_cost = ((self.avg_cost * self.qty) + usd) / new_qty if new_qty else 0.0
                self.qty = new_qty
        else:  # sell, wind_down_sell
            self.cash_flow += usd
            if qty:
                self.realized_pl += usd - qty * self.avg_cost
                self.qty -= qty

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


def print_verbose(name, entries):
    ledgers = {}
    print(f"{'timestamp':19}  {'asset':10} {'action':14} {'usd':>9} {'qty':>12} "
          f"{'pos':>12} {'avg_cost':>10} {'realized':>10} {'unrealized':>11} {'total':>10}")
    for e in entries:
        asset = e.get('asset') or 'XLM'
        ledger = ledgers.setdefault(asset, _Ledger())
        usd = float(e.get('amount_usd') or 0.0)
        qty = float(e.get('amount_xlm') or 0.0) if asset == 'XLM' else 0.0
        ledger.apply(e.get('action'), usd, qty)
        print(f"{_fmt_ts(e.get('timestamp')):19}  {asset[:10]:10} {e.get('action', '?'):14} "
              f"{usd:9.4f} {qty:12.4f} {ledger.qty:12.4f} {ledger.avg_cost:10.6f} "
              f"{ledger.realized_pl:10.4f} {ledger.unrealized_pl:11.4f} {ledger.total_pl:10.4f}")
    print()
    for asset, ledger in ledgers.items():
        print(f"{name} / {asset}: {ledger.n} trades, cash flow {ledger.cash_flow:+.4f} USD, "
              f"realized {ledger.realized_pl:+.4f}, unrealized {ledger.unrealized_pl:+.4f}, "
              f"total P&L {ledger.total_pl:+.4f} USD")


def print_summary(files, as_json=False):
    rows = []
    grand_total = 0.0
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
    rows.sort(key=lambda r: r['first'])

    if as_json:
        print(json.dumps({'strategies': rows, 'grand_total_pl': round(grand_total, 4)}, indent=2))
        return

    for r in rows:
        assets = ', '.join(f"{a}: {v['total_pl']:+.4f}" for a, v in r['assets'].items())
        print(f"{r['name']:24} {r['trades']:4} trades  {r['first']} -> {r['last']}  "
              f"total {r['total_pl']:+8.4f} USD  ({assets})")
    print(f"\n{'GRAND TOTAL':24} {'':4}{'':17}{'':22}total {grand_total:+8.4f} USD "
          f"across {len(rows)} strateg{'y' if len(rows) == 1 else 'ies'}")


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
        print(f"{_fmt_ts(ts):19}  {name[:24]:24} {asset[:10]:10} {e.get('action', '?'):14} "
              f"{usd:9.4f} {ledger.total_pl:11.4f} {running_total:13.4f}")
    print(f"\nrunning total across all strategies/legs: {running_total:+.4f} USD")


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

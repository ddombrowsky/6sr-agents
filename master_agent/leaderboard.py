#!/usr/bin/env python3
"""Print all registered strategies with their status, balances, and net worth.

Reads /opt/strategy_state.json for the registered strategies, each strategy's
own state.json for balance_usd/balance_xlm, and the current XLM/USD price via
tools/price_feed.py to compute net_worth = balance_usd + balance_xlm * price.
"""
import json
import sys
from pathlib import Path

STATE_FILE = Path('/opt/strategy_state.json')

sys.path.append('/opt/tools')
from price_feed import get_price


def load_state():
    if STATE_FILE.exists():
        return json.load(STATE_FILE.open())
    return {}


def load_balances(strategy_path):
    state_path = Path(strategy_path) / 'state.json'
    if not state_path.exists():
        return None, None
    try:
        data = json.load(state_path.open())
        return data.get('balance_usd', 0.0), data.get('balance_xlm', 0.0)
    except Exception:
        return None, None


def main():
    price = get_price()
    if price is None:
        print('Could not fetch current XLM price; net worth will be shown as N/A.')

    state = load_state()
    if not state:
        print('No strategies registered.')
        return

    rows = []
    for name, info in state.items():
        usd, xlm = load_balances(info['path'])
        if usd is None:
            net_worth = None
        elif price is None:
            net_worth = None
        else:
            net_worth = usd + xlm * price
        rows.append((name, info.get('status'), info.get('pid'), usd, xlm, net_worth))

    # Strategies with unknown net worth sort last; known ones descending by net worth.
    rows.sort(key=lambda r: (r[5] is None, -(r[5] or 0)))

    name_w = max(len(r[0]) for r in rows) + 2
    header = f"{'NAME':<{name_w}}{'STATUS':<10}{'PID':<8}{'USD':>12}{'XLM':>14}{'NET WORTH':>14}"
    print(header)
    print('-' * len(header))
    for name, status, pid, usd, xlm, net_worth in rows:
        usd_s = f'{usd:.2f}' if usd is not None else 'N/A'
        xlm_s = f'{xlm:.4f}' if xlm is not None else 'N/A'
        net_s = f'{net_worth:.2f}' if net_worth is not None else 'N/A'
        pid_s = str(pid) if pid else '-'
        print(f'{name:<{name_w}}{status or "unknown":<10}{pid_s:<8}{usd_s:>12}{xlm_s:>14}{net_s:>14}')

    if price is not None:
        print(f'\nCurrent XLM/USD price: {price}')


if __name__ == '__main__':
    main()

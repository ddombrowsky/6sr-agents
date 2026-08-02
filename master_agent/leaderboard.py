#!/usr/bin/env python3
"""Print all registered strategies with their status, balances, and score.

Reads /opt/strategy_state.json for the registered strategies, each strategy's
own state.json for balance_usd/balance_xlm, and the current XLM/USD price via
tools/price_feed.py to compute the score (see score.compute_score).
"""
import json
import sys
from pathlib import Path

STATE_FILE = Path('/opt/strategy_state.json')

sys.path.append('/opt/tools')
from price_feed import get_price

from score import compute_score, compute_score_multi


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


def load_marks(state, price):
    """USD marks for every extra asset any strategy holds. {'XLM': price} on failure."""
    marks = {'XLM': price}
    try:
        import sys
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import dex_price
        import portfolio
    except Exception:
        return marks, None

    specs = set()
    for info in state.values():
        try:
            st = portfolio.normalize_state(json.load(open(Path(info['path']) / 'state.json')))
            specs.update(s for s, p in st['positions'].items()
                         if s != 'XLM' and float(p.get('amount') or 0) > 0)
        except Exception:
            pass
    for spec in sorted(specs):
        mark = dex_price.get_mark_with_depth(spec)
        if mark and mark.get('price'):
            marks[spec] = mark
    return marks, portfolio


def main():
    price = get_price()
    if price is None:
        print('Could not fetch current XLM price; score will be shown as N/A.')

    state = load_state()
    if not state:
        print('No strategies registered.')
        return

    marks, portfolio = load_marks(state, price)

    rows = []
    total_unpriced = 0
    for name, info in state.items():
        usd, xlm = load_balances(info['path'])
        legs, unpriced = 0, []
        score = None
        if usd is not None and price is not None:
            if portfolio is None:
                score = compute_score(usd, xlm, price)
            else:
                try:
                    st = portfolio.normalize_state(
                        json.load(open(Path(info['path']) / 'state.json')))
                    legs = sum(1 for s, p in st['positions'].items()
                               if s != 'XLM' and float(p.get('amount') or 0) > 0)
                    score, unpriced = compute_score_multi(st, marks)
                except Exception:
                    score = compute_score(usd, xlm, price)
        total_unpriced += len(unpriced)
        rows.append((name, info.get('status'), info.get('pid'), usd, xlm, score, legs))

    # Strategies with unknown score sort last; known ones descending by score.
    rows.sort(key=lambda r: (r[5] is None, -(r[5] or 0)))

    name_w = max(len(r[0]) for r in rows) + 2
    header = (f"{'NAME':<{name_w}}{'STATUS':<10}{'PID':<8}{'USD':>12}"
              f"{'XLM':>14}{'LEGS':>6}{'SCORE':>14}")
    print(header)
    print('-' * len(header))
    for name, status, pid, usd, xlm, score, legs in rows:
        usd_s = f'{usd:.2f}' if usd is not None else 'N/A'
        xlm_s = f'{xlm:.4f}' if xlm is not None else 'N/A'
        score_s = f'{score:.2f}' if score is not None else 'N/A'
        pid_s = str(pid) if pid else '-'
        legs_s = str(legs) if legs else '-'
        print(f'{name:<{name_w}}{status or "unknown":<10}{pid_s:<8}{usd_s:>12}'
              f'{xlm_s:>14}{legs_s:>6}{score_s:>14}')

    if price is not None:
        print(f'\nCurrent XLM/USD price: {price}')
    if total_unpriced:
        # Surfaced rather than silently scored as zero: a burst of unpriced legs is a
        # Horizon outage, not every strategy suddenly losing money.
        print(f'WARNING: {total_unpriced} held asset leg(s) had no usable mark and were '
              f'valued at zero.')


if __name__ == '__main__':
    main()

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


def extra_legs(st):
    """[(CODE, amount)] for every non-XLM position held, sorted by code.

    Codes rather than full specs: portfolio.assets_from_config already dedupes a
    strategy's assets by code, so within one row a bare code is unambiguous.
    """
    legs = []
    for spec, position in st['positions'].items():
        if spec == 'XLM':
            continue
        amount = float(position.get('amount') or 0)
        if amount <= 0:
            continue
        legs.append((position.get('code') or spec.split(':')[0], amount))
    return sorted(legs)


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
        legs, unpriced = [], []
        score = None
        if usd is not None and price is not None:
            if portfolio is None:
                score = compute_score(usd, xlm, price)
            else:
                try:
                    st = portfolio.normalize_state(
                        json.load(open(Path(info['path']) / 'state.json')))
                    legs = extra_legs(st)
                    score, unpriced = compute_score_multi(st, marks)
                except Exception:
                    score = compute_score(usd, xlm, price)
        total_unpriced += len(unpriced)
        rows.append((name, info.get('status'), info.get('pid'), score, usd, xlm, legs))

    # Strategies with unknown score sort last; known ones descending by score.
    rows.sort(key=lambda r: (r[3] is None, -(r[3] or 0)))

    name_w = max(len(r[0]) for r in rows) + 2
    header = (f"{'NAME':<{name_w}}{'STATUS':<10}{'PID':<8}{'SCORE':>14}"
              f"{'USD':>12}{'XLM':>14}  {'ASSETS'}")
    print(header)
    print('-' * len(header))
    for name, status, pid, score, usd, xlm, legs in rows:
        score_s = f'{score:.2f}' if score is not None else 'N/A'
        usd_s = f'{usd:.2f}' if usd is not None else 'N/A'
        xlm_s = f'{xlm:.4f}' if xlm is not None else 'N/A'
        pid_s = str(pid) if pid else '-'
        legs_s = ', '.join(f'{code}:{amount:.4f}' for code, amount in legs) or '-'
        print(f'{name:<{name_w}}{status or "unknown":<10}{pid_s:<8}{score_s:>14}'
              f'{usd_s:>12}{xlm_s:>14}  {legs_s}')

    if price is not None:
        print(f'\nCurrent XLM/USD price: {price}')
    if total_unpriced:
        # Surfaced rather than silently scored as zero: a burst of unpriced legs is a
        # Horizon outage, not every strategy suddenly losing money.
        print(f'WARNING: {total_unpriced} held asset leg(s) had no usable mark and were '
              f'valued at zero.')

    blind = backtest_blind(state)
    if blind:
        # An aggregate, not a per-row column: the table would carry a mostly-uninformative
        # flag, while "how much of the population is invisible to its own fitness check"
        # is the number worth knowing. These strategies rank normally -- backtest_strategy
        # just reports config.json's thresholds when asked about them.
        print(f'NOTE: {len(blind)} of {len(state)} strategies are backtest-blind (no '
              f'importable decide(); backtest replays config thresholds instead): '
              f'{", ".join(sorted(blind))}')


def backtest_blind(state):
    """Strategies whose main.py the backtester cannot load a decide() from.

    Fails open (returns nothing) if /opt/tools is unavailable: this is a footnote on a
    read-only table, and a missing module must not turn into a claim about the population.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        from backtest import _is_importable
    except Exception:
        return []
    blind = []
    for name, info in state.items():
        main_py = Path(info.get('path', '')) / 'main.py'
        try:
            if main_py.exists() and not _is_importable(str(main_py)):
                blind.append(name)
        except Exception:
            continue
    return blind


if __name__ == '__main__':
    main()

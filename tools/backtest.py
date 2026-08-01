#!/usr/bin/env python3
"""Replay a strategy against real historical candles and report how it would have done.

Why this exists: until now the only way to evaluate a strategy revision was to start it
and wait an hour for monitor.py to score it -- one extremely noisy sample per cycle,
against whatever the market happened to do in that hour. This module replays a strategy
over ~30 days of real hourly candles (tools/ohlc_history.py) in a fraction of a second,
and reports its return next to buy-and-hold, so a revision can be checked *before* it is
committed and started.

Two ways a strategy's logic is resolved, in order:

1. If <strategy_dir>/main.py defines a top-level `decide(price, history, state, config)`
   function and has no top-level side effects, it is imported and used directly. This is
   the shape revisions are told to write (see REVISION_SYSTEM_PROMPT in master-agent.py).
   `decide` should return None for "no trade", or (side, action, requested_usd) / a dict
   with those keys -- the same triple main.py hands to trade_logger.execute_trade.
2. Otherwise the strategy is treated as the plain threshold bot that template_repo/main.py
   implements: buy `trade_amount_usd` when price <= buy_below, sell that much when
   price >= sell_above, read from config.json. Every strategy currently on disk is this.

Trade execution mirrors trade_logger.execute_trade's clamping exactly (a buy is capped at
available USD, a sell at held XLM, and a zero-size trade is a no-op) so backtest results
are comparable to the live paper numbers in each strategy's state.json. Nothing here
touches trade logs, live.flag, or stellar_trader -- it is pure simulation.

CLI:
    python3 /opt/tools/backtest.py /opt/strategies/<name> [days] [ticks_per_candle]
"""
import ast
import importlib.util
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from ohlc_history import get_candles

START_USD = 1000.0  # same starting balances as template_repo/main.py
START_XLM = 0.0


def _decide_from_config(config):
    """Fallback decide step: template_repo/main.py's threshold rule."""
    buy_below = config.get('buy_below')
    sell_above = config.get('sell_above')
    trade_amount_usd = config.get('trade_amount_usd', 10)

    def decide(price, history, state, cfg):
        if buy_below is not None and price <= buy_below:
            return 'buy', 'buy', trade_amount_usd
        if sell_above is not None and price >= sell_above:
            return 'sell', 'sell', trade_amount_usd
        return None

    return decide


def _is_importable(main_py):
    """True if main.py can be imported without running a trading loop.

    Importing a module runs every top-level statement, and template_repo/main.py's
    top level fetches prices in `while True`, so importing the wrong file would hang
    the backtest forever. Only allow modules whose top level is declarations plus an
    `if __name__ == '__main__'` guard, and which actually define `decide`.
    """
    try:
        tree = ast.parse(main_py.read_text() if hasattr(main_py, 'read_text') else open(main_py).read())
    except Exception:
        return False

    has_decide = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == 'decide':
                has_decide = True
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.Assign,
                             ast.AnnAssign, ast.Pass)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # docstring
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name) and test.left.id == '__name__'):
                continue  # __main__ guard, not executed on import
        return False
    return has_decide


def _load_decide(strategy_dir, config):
    main_py = os.path.join(strategy_dir, 'main.py')
    if not os.path.exists(main_py) or not _is_importable(main_py):
        return _decide_from_config(config), 'config-thresholds'
    try:
        spec = importlib.util.spec_from_file_location(
            f'_bt_{os.path.basename(strategy_dir)}', main_py)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.decide, 'main.py:decide'
    except Exception as e:
        print(f'[backtest] could not import decide from {main_py}: {e}')
        return _decide_from_config(config), 'config-thresholds'


def _normalize(decision):
    """Accept (side, action, usd), a dict, or None."""
    if not decision:
        return None
    if isinstance(decision, dict):
        side = decision.get('side')
        return (side, decision.get('action', side), decision.get('requested_usd', 0)) if side else None
    if isinstance(decision, (tuple, list)) and len(decision) >= 3:
        return decision[0], decision[1], decision[2]
    return None


def backtest(strategy_dir, days=30, ticks_per_candle=1, interval=60):
    """Replay `strategy_dir` over the last `days` of candles.

    ticks_per_candle: live strategies poll every 30s, so an hourly candle is really
    ~120 decision points. Pass 120 to approximate live cadence (only affects how many
    times a standing threshold fires within one candle -- the price is the candle close
    either way, so it does not manufacture intra-candle information).
    """
    config_path = os.path.join(strategy_dir, 'config.json')
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception as e:
            print(f'[backtest] could not read {config_path}: {e}')

    candles = get_candles(hours=days * 24, interval=interval)
    if len(candles) < 2:
        return {'error': 'not enough candle history to backtest'}

    decide, source = _load_decide(strategy_dir, config)

    state = {'balance_usd': START_USD, 'balance_xlm': START_XLM}
    history = []
    trades = sells = wins = 0
    cost_basis = 0.0  # total USD spent on the XLM currently held
    peak = START_USD
    max_drawdown = 0.0

    for candle in candles:
        price = candle['close']
        history.append(price)
        for _ in range(max(1, int(ticks_per_candle))):
            try:
                decision = _normalize(decide(price, history, state, config))
            except Exception as e:
                return {'error': f'decide() raised {type(e).__name__}: {e}', 'decide_source': source}
            if not decision:
                break
            side, _action, requested_usd = decision
            try:
                requested_usd = float(requested_usd)
            except (TypeError, ValueError):
                break

            # Mirrors trade_logger.execute_trade's clamping.
            if side == 'buy':
                actual_usd = min(requested_usd, state['balance_usd'])
                if actual_usd <= 0:
                    break
                state['balance_usd'] -= actual_usd
                state['balance_xlm'] += actual_usd / price
                cost_basis += actual_usd
                trades += 1
            elif side == 'sell':
                actual_xlm = min(requested_usd / price, state['balance_xlm'])
                if actual_xlm <= 0:
                    break
                held = state['balance_xlm']
                # A sell "wins" if it exits above the average price paid for the XLM
                # being sold -- the only definition available without pairing up
                # individual lots, and the one that matches how these bots trade.
                avg_cost = cost_basis / held if held else 0.0
                if price > avg_cost:
                    wins += 1
                cost_basis -= avg_cost * actual_xlm
                state['balance_xlm'] -= actual_xlm
                state['balance_usd'] += actual_xlm * price
                trades += 1
                sells += 1
            else:
                break

        net_worth = state['balance_usd'] + state['balance_xlm'] * price
        peak = max(peak, net_worth)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - net_worth) / peak)

    final_price = candles[-1]['close']
    final_net_worth = state['balance_usd'] + state['balance_xlm'] * final_price
    buy_hold = START_USD * final_price / candles[0]['close']

    return {
        'strategy': os.path.basename(strategy_dir.rstrip('/')),
        'decide_source': source,
        'candles': len(candles),
        'days': days,
        'trades': trades,
        'sells': sells,
        'win_rate': round(wins / sells, 4) if sells else None,  # over sells only
        'final_usd': round(state['balance_usd'], 4),
        'final_xlm': round(state['balance_xlm'], 4),
        'final_net_worth': round(final_net_worth, 2),
        'return_pct': round((final_net_worth / START_USD - 1) * 100, 3),
        'buy_hold_pct': round((buy_hold / START_USD - 1) * 100, 3),
        'beats_buy_hold': final_net_worth > buy_hold,
        'max_drawdown_pct': round(max_drawdown * 100, 3),
    }


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1])
        sys.exit(1)
    path = sys.argv[1]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    ticks = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    print(json.dumps(backtest(path, days=days, ticks_per_candle=ticks), indent=2))

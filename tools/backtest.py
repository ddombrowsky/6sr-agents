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

Extra (non-XLM) assets: pass legs=True, or call backtest_asset() directly, to replay each
declared extra asset over its own Stellar DEX candle history. Those results are reported
separately under `legs` and are NEVER folded into `beats_buy_hold`, which stays a pure
XLM-leg measure. That is a correctness boundary, not a stylistic one: DEX history for a
thinly-traded asset is sparse and its VWAPs are noisy, so letting it into the gate would
let book noise approve a revision and would silently change what the revision prompt's
"treat beats_buy_hold: false as a failed revision" instruction means.

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


_TOP_LEVEL_RULE = ('the module top level may only contain imports, assignments, defs, '
                   "the docstring and an `if __name__` guard")


def _source_of(main_py):
    """main.py's text, whether given as a path, a Path, or the source itself."""
    if hasattr(main_py, 'read_text'):
        return main_py.read_text()
    if isinstance(main_py, str) and not os.path.exists(main_py):
        return main_py  # already source, not a path
    return open(main_py).read()


def _describe(node):
    """One short line naming a top-level statement, for the rejection reason."""
    try:
        text = ast.unparse(node).splitlines()[0]
    except Exception:  # ast.unparse is 3.9+; never let the reason string be the failure
        text = ''
    if len(text) > 60:
        text = text[:57] + '...'
    return f'{type(node).__name__} at line {getattr(node, "lineno", "?")}' + (f': {text}' if text else '')


def importability_report(main_py):
    """(ok, reason) for whether the backtester can import decide() from this main.py.

    The rules are _is_importable's, unchanged -- this only adds the reason, because the
    caller that rejects a revision over this has to be able to say which line did it.
    "It is not importable" is unactionable; "top-level Expr at line 9:
    sys.path.append('/opt/tools')" is the whole fix.

    Accepts a path, a Path, or the source text itself, so a caller holding a candidate
    from `git show` doesn't have to write it to disk first.
    """
    try:
        tree = ast.parse(_source_of(main_py))
    except SyntaxError as e:
        return False, f'main.py has a syntax error: {e.msg} (line {e.lineno})'
    except Exception as e:
        return False, f'main.py could not be read: {e}'

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
        return False, f'top-level {_describe(node)} -- {_TOP_LEVEL_RULE}'
    if not has_decide:
        return False, 'no top-level decide() function'
    return True, 'decide() is importable'


def _is_importable(main_py):
    """True if main.py can be imported without running a trading loop.

    Importing a module runs every top-level statement, and template_repo/main.py's
    top level fetches prices in `while True`, so importing the wrong file would hang
    the backtest forever. Only allow modules whose top level is declarations plus an
    `if __name__ == '__main__'` guard, and which actually define `decide`.
    """
    return importability_report(main_py)[0]


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


def _fresh_state():
    """A starting state in the same shape a live strategy sees.

    Falls back to the flat two-key dict if portfolio isn't importable, so the backtester
    keeps working standalone -- it is a diagnostic tool and must not become the reason a
    revision can't be evaluated.
    """
    state = {'balance_usd': START_USD, 'balance_xlm': START_XLM}
    try:
        import portfolio
        return portfolio.normalize_state(state)
    except Exception:
        return state


def _add_xlm(state, delta):
    """Move the XLM leg by `delta`, keeping `balance_xlm` and positions consistent.

    Only the XLM leg is simulated. Extra assets are deliberately not replayed here:
    their history comes from Stellar's own DEX, is far thinner and gappier than the
    Kraken/Coinbase XLM candles this uses, and folding it into `beats_buy_hold` would
    let VWAP noise on a $20 book decide whether a revision passes its fitness gate.
    """
    try:
        import portfolio
        if 'positions' in state:
            portfolio.add_amount(state, 'XLM', delta)
            return
    except Exception:
        pass
    state['balance_xlm'] = max(0.0, state['balance_xlm'] + delta)


def _load_decide_asset(strategy_dir):
    """The strategy's optional per-asset decide function, or None.

    Same import path and same importability rules as `decide`. Absent, the leg is
    replayed against its own buy_below/sell_above from config.json, which is exactly
    what template_repo/main.py does when decide_asset is not defined -- so the backtest
    matches the live behavior either way.
    """
    main_py = os.path.join(strategy_dir, 'main.py')
    if not os.path.exists(main_py) or not _is_importable(main_py):
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            f'_bta_{os.path.basename(strategy_dir)}', main_py)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, 'decide_asset', None)
    except Exception:
        return None


def backtest_asset(strategy_dir, code, issuer, days=7, ticks_per_candle=1):
    """Replay ONE extra (non-XLM) leg over its own DEX candle history.

    Reported separately from the XLM leg and never folded into `beats_buy_hold`. That
    separation is deliberate rather than tidiness: this history comes from Stellar's own
    DEX via /trade_aggregations, where an asset with a few hundred dollars of daily
    volume produces sparse buckets and noisy VWAPs. Letting that decide whether a
    revision passes its fitness gate would let noise on a thin book approve a strategy,
    and would silently change what "beats_buy_hold: false is a failed revision" means.

    Treat the numbers here as indicative. Returns a dict, or {'error': ...}.
    """
    import assets
    import dex_price

    try:
        spec = assets.canonical(code, issuer)
    except Exception as e:
        return {'error': f'malformed asset: {e}'}

    config = {}
    config_path = os.path.join(strategy_dir, 'config.json')
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception:
            pass

    leg = None
    try:
        import portfolio
        for candidate in portfolio.assets_from_config(config):
            if candidate['spec'] == spec:
                leg = candidate
                break
    except Exception:
        pass
    if leg is None:
        leg = {'code': code, 'issuer': issuer, 'spec': spec,
               'buy_below': 0.0, 'sell_above': 0.0,
               'trade_amount_usd': config.get('trade_amount_usd', 10)}

    candles = dex_price.get_candles(spec, hours=days * 24)
    if len(candles) < 2:
        return {'error': f'not enough DEX candle history for {assets.display(spec)}',
                'spec': spec, 'candles': len(candles)}

    decide_asset = _load_decide_asset(strategy_dir)
    source = 'main.py:decide_asset' if decide_asset else 'config-thresholds'
    if decide_asset is None:
        def decide_asset(asset, price, history, state, config):
            if asset.get('buy_below') and price <= asset['buy_below']:
                return ('buy', 'buy', asset.get('trade_amount_usd', 10))
            if asset.get('sell_above') and price >= asset['sell_above']:
                return ('sell', 'sell', asset.get('trade_amount_usd', 10))
            return None

    state = _fresh_state()
    history = []
    trades = sells = 0

    for candle in candles:
        price = candle['close']
        history.append(price)
        for _ in range(max(1, int(ticks_per_candle))):
            try:
                decision = _normalize(decide_asset(leg, price, history, state, config))
            except Exception as e:
                return {'error': f'decide_asset() raised {type(e).__name__}: {e}',
                        'spec': spec, 'decide_source': source}
            if not decision:
                break
            side, _action, requested_usd = decision
            try:
                requested_usd = float(requested_usd)
            except (TypeError, ValueError):
                break
            if price is None or price != price or price <= 0:
                break

            held = _leg_amount(state, spec)
            if side == 'buy':
                actual_usd = min(requested_usd, state['balance_usd'])
                if actual_usd <= 0:
                    break
                state['balance_usd'] -= actual_usd
                _add_leg(state, spec, actual_usd / price)
                trades += 1
            elif side == 'sell':
                actual = min(requested_usd / price, held)
                if actual <= 0:
                    break
                _add_leg(state, spec, -actual)
                state['balance_usd'] += actual * price
                trades += 1
                sells += 1
            else:
                break

    final_price = candles[-1]['close']
    final_net_worth = state['balance_usd'] + _leg_amount(state, spec) * final_price
    buy_hold = START_USD * final_price / candles[0]['close']
    traded_buckets = sum(1 for c in candles if c.get('trade_count', 0) > 0)

    return {
        'spec': spec,
        'decide_source': source,
        'candles': len(candles),
        'days': days,
        # How much of the window actually had trades. A low number means the return
        # figures rest on very little real price discovery.
        'active_buckets': traded_buckets,
        'trades': trades,
        'sells': sells,
        'final_usd': round(state['balance_usd'], 4),
        'final_amount': round(_leg_amount(state, spec), 4),
        'final_net_worth': round(final_net_worth, 2),
        'return_pct': round((final_net_worth / START_USD - 1) * 100, 3),
        'buy_hold_pct': round((buy_hold / START_USD - 1) * 100, 3),
        # Reported for information only. beats_buy_hold on the XLM leg is the gate.
        'beats_buy_hold_leg_only': final_net_worth > buy_hold,
    }


def _leg_amount(state, spec):
    try:
        import portfolio
        return portfolio.get_amount(state, spec)
    except Exception:
        return 0.0


def _add_leg(state, spec, delta):
    try:
        import portfolio
        portfolio.add_amount(state, spec, delta)
    except Exception:
        pass


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


def backtest(strategy_dir, days=30, ticks_per_candle=1, interval=60, legs=False):
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

    # Normalized to the same shape a live strategy is handed. decide() receives this
    # dict, and live it now arrives with a `positions` map (trade_logger.execute_trade
    # normalizes on entry). Without this a decide() that reads state['positions'] would
    # work in production and KeyError only under backtest -- a divergence that would
    # show up as a "failed revision" pointing at the wrong cause.
    state = _fresh_state()
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

            # Mirrors trade_logger.execute_trade's clamping, including its guards: a
            # non-positive or nan price is refused rather than dividing by it, and a
            # nan size is refused rather than poisoning the balances.
            if price is None or price != price or price <= 0:
                break
            if requested_usd != requested_usd:
                break

            if side == 'buy':
                actual_usd = min(requested_usd, state['balance_usd'])
                if actual_usd <= 0:
                    break
                state['balance_usd'] -= actual_usd
                _add_xlm(state, actual_usd / price)
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
                _add_xlm(state, -actual_xlm)
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

    result = {
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
        # Computed on the XLM leg ONLY, deliberately. Extra assets are replayed against
        # thin, gappy DEX history; folding them in here would let VWAP noise on a small
        # book decide whether a revision passes, and would quietly change what the
        # revision prompt's "treat beats_buy_hold: false as a failed revision" means.
        'beats_buy_hold': final_net_worth > buy_hold,
        'max_drawdown_pct': round(max_drawdown * 100, 3),
    }

    if legs:
        result['legs'] = _backtest_legs(strategy_dir, config, days)
    return result


def _backtest_legs(strategy_dir, config, days):
    """Replay each declared extra asset separately. [] if none or unavailable."""
    try:
        import portfolio
        declared = portfolio.assets_from_config(config)
    except Exception:
        return []
    out = []
    for leg in declared:
        # Extra legs get a shorter window than the XLM leg: DEX history is sparse
        # enough that asking for 30 days mostly returns empty buckets.
        out.append(backtest_asset(strategy_dir, leg['code'], leg['issuer'],
                                  days=min(days, 7)))
    return out


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1])
        sys.exit(1)
    path = sys.argv[1]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    ticks = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    print(json.dumps(backtest(path, days=days, ticks_per_candle=ticks, legs=True), indent=2))

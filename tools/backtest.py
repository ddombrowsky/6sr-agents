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

That mirroring now includes COST (2026-08-03). Both this and execute_trade fill through
friction.fill_price, so a buy lifts the ask and a sell hits the bid. Before that, a
strategy could "beat buy-and-hold" purely by trading more, and the loop dutifully
selected for exactly that: the leader was turning $5,757 of volume into a $23.33 gain, a
40.5 bp edge against a 12.6 bp book nobody charged it for. Buy-and-hold is charged ONE
entry half-spread, because it too has to be bought once -- charging the strategy and not
the benchmark would replace one biased comparison with its mirror image. The result now
carries `friction_bp` and `total_friction_usd` so a revision can see what its own
turnover cost it.

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


def _friction_half(spec):
    """Half-spread for `spec`, or 0.0 if friction.py cannot be consulted.

    Note this is the ONE place in the friction work that fails *open* rather than
    charging a floor, and the asymmetry with trade_logger is deliberate. There, refusing
    to charge would let real evolution proceed on a false premise. Here, a missing
    tools module would silently make every backtest in the population report a number
    computed under different assumptions than the last one -- and a backtest that
    quietly changes its own units is worse than one that is visibly uncharged, because
    `friction_bp: 0.0` in the result says exactly what happened.
    """
    try:
        import friction
        return friction.half_spread(spec)
    except Exception:
        return 0.0


def _fill(side, price, half):
    """price adjusted for crossing the book. Pure, inlined rather than calling
    friction.fill_price, because this runs tens of thousands of times per replay."""
    return price * (1.0 + half) if side == 'buy' else price * (1.0 - half)


# How much evidence the basis metrics need before they are allowed to claim anything.
# Below either threshold `beats_basis_null` reports None rather than False: an unmeasured
# claim and a refuted one are different, and conflating them is how a strategy gets
# reverted for a signal nobody actually looked at.
MIN_EDGE_SAMPLES = 10
MIN_BASIS_COVERAGE = 0.5


def _basis_series(hours):
    """Recorded (ts, basis_bp, tradeable_bp) rows for XLM, oldest first. [] on failure.

    Fails OPEN, exactly like _friction_half above and for the same reason: a missing
    tools module must leave the backtest reporting an honest zero-coverage result rather
    than refusing to run. Zero coverage is visible in the payload; a hard failure here
    would take out every revision's fitness check at once.
    """
    try:
        import market_recorder
        rows = market_recorder.read_history(hours=hours, spec='XLM')
    except Exception:
        return []
    out = []
    for row in rows:
        try:
            if row.get('ts') and row.get('basis_bp') is not None:
                out.append((float(row['ts']), float(row['basis_bp']),
                            row.get('tradeable_bp')))
        except Exception:
            continue
    out.sort(key=lambda r: r[0])
    return out


def _basis_join(candles, series, bucket_s):
    """One recorded basis row (or None) per candle, aligned to each candle's CLOSE.

    A candle's `ts` is its bucket START on both Kraken and Coinbase, so the moment a
    strategy would have acted on `close` is ts + bucket_s. The rule is therefore the
    LAST row at or before that instant, within a tolerance -- never the nearest row in
    absolute time. Nearest-match would happily pick a reading from after the close and
    feed the replay information the live strategy could not have had; on a 1-minute grid
    that is up to 30 seconds of look-ahead, which is an eternity for a mean-reverting
    signal and would make a useless strategy backtest beautifully.

    Tolerance is max(bucket_s, 300): a 1-minute grid against 1-minute rows must not be
    thrown off by a few seconds of recorder drift, and no grid should reuse a reading
    older than five minutes.
    """
    if not series:
        return [None] * len(candles)
    tolerance = max(bucket_s, 300)
    out = []
    i = 0
    n = len(series)
    for candle in candles:
        close_ts = float(candle.get('ts', 0)) + bucket_s
        while i < n and series[i][0] <= close_ts:
            i += 1
        # series[i-1] is now the last row at or before close_ts, if there is one.
        if i == 0:
            out.append(None)
            continue
        ts, basis_bp, tradeable_bp = series[i - 1]
        out.append((basis_bp, tradeable_bp, ts) if close_ts - ts <= tolerance else None)
    return out


def basis_edge_stats(edges, buys, sells, population_basis, coverage=1.0):
    """The criterion-3 metric: did these fills happen at better venue moments than luck?

    `beats_buy_hold` cannot answer that -- it is directional, and the basis claim is not
    a claim about direction. The claim is "my entries are timed against the DEX/CEX
    dislocation", and the honest null for it is the same statistic computed over every
    moment in the window rather than the moments this strategy chose.

    A basis-blind strategy's fill times are uncorrelated with the basis, so its expected
    advantage is -mean(basis) per buy and +mean(basis) per sell; weighting those by the
    strategy's OWN realized buy/sell mix is what makes the two numbers comparable. The
    null is not assumed to be zero, and must not be: it is ~0 over a long window but
    emphatically not over the 12h one a 1-minute replay can reach, where a drifting
    basis would otherwise be credited to whichever side traded more.

    This lives here, and basis_report.py imports it, so the replayed metric and the
    live-log metric cannot drift into being two different statistics with one name.
    """
    n = len(edges)
    out = {
        'basis_edge_n': n,
        'basis_edge_bp': None,
        'basis_edge_null_bp': None,
        'basis_edge_excess_bp': None,
        'basis_edge_t': None,
        'beats_basis_null': None,
    }
    if not n or population_basis is None:
        return out

    mean_edge = sum(edges) / n
    null = population_basis * (sells - buys) / float(buys + sells) if (buys + sells) else 0.0
    excess = mean_edge - null
    out['basis_edge_bp'] = round(mean_edge, 3)
    out['basis_edge_null_bp'] = round(null, 3)
    out['basis_edge_excess_bp'] = round(excess, 3)

    if n > 1:
        try:
            import statistics
            stdev = statistics.stdev(edges)
            if stdev > 0:
                out['basis_edge_t'] = round(excess / (stdev / (n ** 0.5)), 2)
        except Exception:
            pass

    # None, not False, below either threshold -- see MIN_EDGE_SAMPLES.
    if n >= MIN_EDGE_SAMPLES and coverage >= MIN_BASIS_COVERAGE:
        out['beats_basis_null'] = excess > 0
    return out


def _apply_basis(state, row):
    """Put a joined basis row into `state`, or take the previous one back out.

    The pop matters as much as the set. state persists across candles here exactly as
    state.json persists across ticks live, so leaving a stale reading in place would let
    a gap in the recorded history read as a confident basis that happens to be old --
    the one failure mode neither the live loop nor the replay can detect from inside
    decide(). Absent means unknown, and every caller is written to treat unknown as
    neutral.
    """
    if row is None:
        state.pop('basis_bp', None)
        state.pop('basis_tradeable_bp', None)
        state.pop('basis_ts', None)
        return
    basis_bp, tradeable_bp, ts = row
    state['basis_bp'] = basis_bp
    state['basis_tradeable_bp'] = tradeable_bp
    state['basis_ts'] = ts


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
    state = {'balance_usd': START_USD, 'balance_xlm': START_XLM,
              'borrowed_xlm': 0.0, 'short_proceeds_usd': 0.0}
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

    # This matters far more here than on the XLM leg. Measured 2026-08-03, XLM's book was
    # 8.7 bp wide while AQUA's was 151 bp and ARS's 195 bp -- 17-22x. A leg strategy
    # replayed without cost looks like a diversification win and is in fact a way to pay
    # a 1.5% toll several times an hour, which is how "extra assets" would have entered
    # the population looking profitable.
    half = _friction_half(spec)
    friction_usd = 0.0

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
            fill = _fill(side, price, half)
            if side == 'buy':
                actual_usd = min(requested_usd, state['balance_usd'])
                if actual_usd <= 0:
                    break
                state['balance_usd'] -= actual_usd
                _add_leg(state, spec, actual_usd / fill)
                friction_usd += actual_usd * half
                trades += 1
            elif side == 'sell':
                actual = min(requested_usd / fill, held)
                if actual <= 0:
                    break
                _add_leg(state, spec, -actual)
                state['balance_usd'] += actual * fill
                friction_usd += actual * fill * half
                trades += 1
                sells += 1
            else:
                break

    final_price = candles[-1]['close']
    final_net_worth = state['balance_usd'] + _leg_amount(state, spec) * final_price
    buy_hold = START_USD * final_price / _fill('buy', candles[0]['close'], half)
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
        'friction_bp': round(half * 10000, 2),
        'total_friction_usd': round(friction_usd, 4),
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

    candles = get_candles(hours=float(days) * 24, interval=interval)
    if len(candles) < 2:
        return {'error': 'not enough candle history to backtest'}

    # The recorded DEX/CEX basis, joined to the candle grid. Asks for a slightly wider
    # window than the replay so the first candle can find a row at or before its close.
    bucket_s = max(1, int(interval)) * 60
    basis_rows = _basis_join(candles, _basis_series(float(days) * 24 + 1), bucket_s)
    basis_matched = sum(1 for r in basis_rows if r is not None)

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

    # Fetched ONCE for the whole replay, not per tick: a 30-day run at 120 ticks/candle
    # is ~86,000 decisions, and friction.half_spread would otherwise hit its cache that
    # many times for a number that moves on the scale of hours. Passing it as `h` also
    # keeps the whole backtest deterministic against a single book reading, so two runs
    # of the same strategy are comparable to each other.
    half = _friction_half('XLM')
    friction_usd = 0.0

    # Per-fill venue advantage, in bp, plus the buy/sell mix it came from -- the null
    # baseline has to be weighted by that mix. See the basis_edge_* keys in the result.
    basis_edges = []
    basis_buys = basis_sells = 0

    # Shorting (SHORTING_PLAN.md): mirrors trade_logger.execute_trade's borrow/repay
    # logic so a backtested return stays comparable to live paper numbers.
    allow_shorting = bool(config.get('allow_shorting', False))
    try:
        import portfolio as _portfolio_mod
        max_borrowed_xlm = _portfolio_mod.MAX_BORROWED_XLM
    except Exception:
        max_borrowed_xlm = 6000.0

    for candle, basis_row in zip(candles, basis_rows):
        price = candle['close']
        history.append(price)
        # Before the tick loop, exactly where template_repo/main.py sets it before
        # calling decide(): the basis is an input to the decision, not a consequence.
        _apply_basis(state, basis_row)
        basis_bp = basis_row[0] if basis_row else None
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

            # The fill, not the quote -- same rule as trade_logger.execute_trade.
            fill = _fill(side, price, half)

            if side == 'buy':
                actual_usd = min(requested_usd, state['balance_usd'])
                if actual_usd <= 0:
                    break
                state['balance_usd'] -= actual_usd
                amount_xlm = actual_usd / fill
                # Repay any outstanding short before growing the real position --
                # always, not gated on allow_shorting. See trade_logger.execute_trade.
                borrowed = state.get('borrowed_xlm', 0.0)
                repay = min(amount_xlm, borrowed)
                if repay > 0:
                    state['borrowed_xlm'] = borrowed - repay
                    proceeds = state.get('short_proceeds_usd', 0.0)
                    state['short_proceeds_usd'] = max(0.0, proceeds - repay * fill)
                    amount_xlm -= repay
                if amount_xlm > 0:
                    _add_xlm(state, amount_xlm)
                    cost_basis += amount_xlm * fill
                friction_usd += actual_usd * half
                trades += 1
                # A negative basis means the DEX is cheap against the CEX, which is
                # good for a buyer -- hence the sign flip. Sells take it unflipped.
                if basis_bp is not None:
                    basis_edges.append(-basis_bp)
                    basis_buys += 1
            elif side == 'sell':
                held = state['balance_xlm']
                if allow_shorting:
                    borrowed = state.get('borrowed_xlm', 0.0)
                    headroom = max(0.0, max_borrowed_xlm - borrowed)
                else:
                    headroom = 0.0
                actual_xlm = min(requested_usd / fill, held + headroom)
                if actual_xlm <= 0:
                    break
                from_held = min(actual_xlm, held)
                from_borrow = actual_xlm - from_held
                # A sell "wins" if it exits above the average price paid for the XLM
                # being sold -- the only definition available without pairing up
                # individual lots, and the one that matches how these bots trade.
                # Measured on the FILL: a sell that clears the quote but not the spread
                # is a loss, and calling it a win is how a win_rate of 60% coexisted
                # with a strategy that bled. Only the held portion has a cost basis to
                # be measured against -- opening a short isn't exiting a lot.
                avg_cost = cost_basis / held if held else 0.0
                if from_held > 0 and fill > avg_cost:
                    wins += 1
                cost_basis -= avg_cost * from_held
                _add_xlm(state, -from_held)
                if from_borrow > 0:
                    state['borrowed_xlm'] = state.get('borrowed_xlm', 0.0) + from_borrow
                    state['short_proceeds_usd'] = (
                        state.get('short_proceeds_usd', 0.0) + from_borrow * fill)
                state['balance_usd'] += actual_xlm * fill
                friction_usd += actual_xlm * fill * half
                trades += 1
                sells += 1
                if basis_bp is not None:
                    basis_edges.append(basis_bp)
                    basis_sells += 1
            else:
                break

        # The short liability is what it would cost to buy back borrowed_xlm right now
        # -- omitting it would let a strategy that never covers look like it kept the
        # sale proceeds for free. Mirrors master_agent/score.py's compute_score_multi.
        net_worth = (state['balance_usd'] + state['balance_xlm'] * price
                     - state.get('borrowed_xlm', 0.0) * price)
        peak = max(peak, net_worth)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - net_worth) / peak)

    final_price = candles[-1]['close']
    final_net_worth = (state['balance_usd'] + state['balance_xlm'] * final_price
                        - state.get('borrowed_xlm', 0.0) * final_price)
    # Buy-and-hold pays to get in, once. It is a real alternative a real operator could
    # take, not a costless abstraction, and exempting it while charging the strategy
    # would just invert the old bias instead of removing it. One entry, no exit: the
    # comparison is against a position still held at final_price, exactly as before.
    buy_hold = START_USD * final_price / _fill('buy', candles[0]['close'], half)

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
        'final_borrowed_xlm': round(state.get('borrowed_xlm', 0.0), 4),
        'final_net_worth': round(final_net_worth, 2),
        'return_pct': round((final_net_worth / START_USD - 1) * 100, 3),
        'buy_hold_pct': round((buy_hold / START_USD - 1) * 100, 3),
        # Computed on the XLM leg ONLY, deliberately. Extra assets are replayed against
        # thin, gappy DEX history; folding them in here would let VWAP noise on a small
        # book decide whether a revision passes, and would quietly change what the
        # revision prompt's "treat beats_buy_hold: false as a failed revision" means.
        'beats_buy_hold': final_net_worth > buy_hold,
        'max_drawdown_pct': round(max_drawdown * 100, 3),
        # What trading cost, so a revision can weigh turnover against edge instead of
        # treating volume as free. If total_friction_usd rivals the gain, the strategy
        # is not a strategy, it is a fee generator.
        'friction_bp': round(half * 10000, 2),
        'total_friction_usd': round(friction_usd, 4),
        # How much of this replay actually had a recorded basis behind it. Read this
        # BEFORE reading anything else basis-related: the recorder only started keeping
        # a per-minute series recently, so a 30-day run is mostly uncovered and a
        # confident-looking basis_edge_excess_bp there rests on a handful of candles.
        # The field exists because decide_source spent weeks in the payload unread.
        'basis_coverage': round(basis_matched / len(candles), 4) if candles else 0.0,
        'basis_candles': basis_matched,
    }
    result.update(basis_edge_stats(
        basis_edges, basis_buys, basis_sells,
        population_basis=(sum(r[0] for r in basis_rows if r) / basis_matched
                          if basis_matched else None),
        coverage=result['basis_coverage'],
    ))

    if legs:
        result['legs'] = _backtest_legs(strategy_dir, config, days)
    return result


def backtest_basis(strategy_dir, hours=12, ticks_per_candle=2):
    """Replay `strategy_dir` on a 1-minute grid, to see its basis logic actually fire.

    The default 30-day hourly replay cannot evaluate a basis strategy at all: the signal
    mean-reverts on a scale of minutes, and one reading per hour flattens it into noise.
    This runs the same code over 1-minute candles instead.

    Read `basis_edge_excess_bp` and `beats_basis_null` from this run, NOT
    `beats_buy_hold`. Twelve hours of return is twelve hours of beta -- it says what XLM
    did this morning, not what the strategy is worth, and treating it as a fitness
    verdict would be a worse mistake than not running this at all.

    Twelve hours is a ceiling, not a default to raise: ohlc_history's sources cap at 720
    (Kraken) and 300 (Coinbase) candles per request, so a 1-minute grid runs out of
    upstream history there regardless of how much basis we have recorded.
    """
    result = backtest(strategy_dir, days=float(hours) / 24.0,
                      ticks_per_candle=ticks_per_candle, interval=1, legs=False)
    if isinstance(result, dict):
        result['window_hours'] = hours
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
    argv = sys.argv[1:]
    interval = 60
    if '--interval' in argv:
        i = argv.index('--interval')
        interval = int(argv[i + 1])
        del argv[i:i + 2]
    path = argv[0]
    # float, so `--interval 1 0.5` (a half-day 1-minute replay) is expressible.
    days = float(argv[1]) if len(argv) > 1 else 30
    ticks = int(argv[2]) if len(argv) > 2 else 1
    print(json.dumps(backtest(path, days=days, ticks_per_candle=ticks,
                              interval=interval, legs=True), indent=2))

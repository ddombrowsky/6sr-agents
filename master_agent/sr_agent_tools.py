import ast
import json
import operator
import subprocess
import sys
from datetime import datetime

sys.path.append('/opt/tools')


def get_uptime() -> str:
    result = subprocess.run(['uptime'], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except Exception as e:
        return f'error: {e}'


def write_file(path: str, content: str) -> str:
    try:
        with open(path, 'w') as f:
            f.write(content)
        return f'wrote {len(content)} bytes to {path}'
    except Exception as e:
        return f'error: {e}'


def fetch_url(url: str) -> str:
    try:
        result = subprocess.run(['curl', '-sL', url], capture_output=True, text=True, check=True, timeout=20)
        return result.stdout
    except subprocess.TimeoutExpired:
        return 'error: timed out after 20s'
    except Exception as e:
        return f'error: {e}'


def install_package(package: str) -> str:
    try:
        result = subprocess.run(['apt-get', 'install', '-y', package], capture_output=True, text=True, check=True, timeout=300)
        return result.stdout
    except subprocess.TimeoutExpired:
        return 'error: timed out after 300s'
    except Exception as e:
        return f'error: {e}'


def update_package_list() -> str:
    try:
        result = subprocess.run(['apt-get', 'update'], capture_output=True, text=True, check=True, timeout=300)
        return result.stdout
    except subprocess.TimeoutExpired:
        return 'error: timed out after 300s'
    except Exception as e:
        return f'error: {e}'


_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}


def _eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f'unsupported expression: {ast.dump(node)}')


def calculate(expression: str) -> str:
    try:
        return str(_eval_node(ast.parse(expression, mode='eval').body))
    except Exception as e:
        return f'error: {e}'


def get_current_time() -> str:
    return datetime.now().isoformat()


def exec(command: str) -> str:
    try:
        result = subprocess.run(
            ['/bin/sh', '-c', command],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            output += f'\n[exit code {result.returncode}]'
        return output
    except subprocess.TimeoutExpired:
        return 'error: timed out after 120s'
    except Exception as e:
        return f'error: {e}'


def backtest_strategy(strategy_path: str, days: float = 30, ticks_per_candle: int = 1,
                      interval: int = 60) -> str:
    """Replay a strategy over real historical candles and return a JSON result summary.

    Imported lazily so a broken/missing /opt/tools module can never stop the agent from
    starting -- the same reason the price feed is imported inside monitor.py's helper.

    `days` is a float and `interval` is the candle size in minutes so that a basis-aware
    revision can ask for the only grid its logic is visible on: interval=1, days=0.5.
    """
    try:
        from backtest import backtest
        # legs=True costs nothing for an XLM-only strategy (no declared assets means no
        # extra work) and is what makes a multi-asset revision reviewable at all.
        result = backtest(strategy_path, days=float(days),
                          ticks_per_candle=int(ticks_per_candle),
                          interval=int(interval), legs=True)
        # decide_source has always been in the payload, and was never noticed: models read
        # the field they came for (beats_buy_hold), which the prompt tells them is
        # authoritative -- and which, on a config-thresholds replay, is a confident number
        # about config.json rather than about the code being revised. On 2026-08-03 that
        # described 122 of 130 strategies. Loud, top-level, and impossible to skim past.
        if isinstance(result, dict) and result.get('decide_source') != 'main.py:decide':
            result['WARNING'] = (
                f"decide_source is {result.get('decide_source')!r}: the backtester could "
                f"NOT import decide() from main.py, so every number here (return_pct, "
                f"beats_buy_hold, win_rate, max_drawdown_pct) describes config.json's "
                f"buy_below/sell_above thresholds, NOT this strategy's code. Fix the "
                f"structure first: main.py's top level may contain only imports, "
                f"assignments, defs, the docstring and an `if __name__ == '__main__'` "
                f"guard, with the logic in a top-level "
                f"decide(price, history, state, config). Then re-run this tool and check "
                f"that decide_source is 'main.py:decide' before trusting any result.")
        # Same failure shape, one signal over: a strategy that gates on the basis but is
        # replayed over candles with no recorded basis behind them backtests identically
        # to one that ignores it, and the result looks like a clean verdict on logic
        # that never ran. Coverage is the field that says so.
        if isinstance(result, dict) and 'basis_coverage' in result:
            coverage = result.get('basis_coverage') or 0.0
            if coverage < 0.5:
                result['BASIS_WARNING'] = (
                    f"basis_coverage is {coverage}: only {result.get('basis_candles', 0)} "
                    f"of {result.get('candles')} candles had a recorded DEX/CEX basis, so "
                    f"any basis logic in this strategy was inert for the rest of the "
                    f"replay and basis_edge_excess_bp/beats_basis_null are not "
                    f"conclusions. The recorded series is per-minute and recent: use "
                    f"interval=1 with days=0.5 to replay on a grid it actually covers.")
        return json.dumps(result)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def get_price_history(hours: int = 720, interval: int = 60) -> str:
    """Recent XLM/USD close prices as a JSON list, oldest first."""
    try:
        from ohlc_history import closes
        return json.dumps(closes(hours=int(hours), interval=int(interval)))
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def backtest_forecast_strategy(strategy_path: str, n: int = 2000) -> str:
    """Replay a forecasting strategy over a fixed set of resolved questions.

    The forecast-domain analogue of backtest_strategy: same role (a real fitness check
    to iterate against before committing a revision), different domain -- there is no
    price here, only a per-question `feature` and a Brier score against the outcome.
    Imported lazily for the same reason backtest_strategy is: a broken/missing
    /opt/tools module must never stop the agent from starting.
    """
    try:
        from forecast_backtest import replay
        result = replay(strategy_path, n=int(n))
        if isinstance(result, dict) and result.get('decide_source') not in (None, 'main.py:decide'):
            result['WARNING'] = (
                f"decide_source is {result.get('decide_source')!r}: the backtester could "
                f"NOT import decide() from main.py, so every number here describes the "
                f"mechanical confidence_gain-only fallback, NOT this strategy's code. "
                f"Fix the structure first: main.py's top level may contain only imports, "
                f"assignments, defs, the docstring and an `if __name__ == '__main__'` "
                f"guard, with the logic in a top-level "
                f"decide(feature, history, state, config). Then re-run this tool and "
                f"check that decide_source is 'main.py:decide' before trusting any result.")
        return json.dumps(result)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def backtest_kalshi_strategy(strategy_path: str, rebuild: bool = False) -> str:
    """Replay a Kalshi strategy over a fixed set of already-resolved real markets.

    The kalshi-domain analogue of backtest_forecast_strategy: same role, different
    domain -- there is no synthetic seed here, only real Kalshi markets that have
    already settled (see kalshi_backtest.py's module docstring for what "fixed" means
    for a set built by crawling a live exchange rather than a generative model).
    Imported lazily for the same reason backtest_strategy is.
    """
    try:
        from kalshi_backtest import build_backtest_set, replay
        examples = build_backtest_set(force=bool(rebuild)) if rebuild else None
        result = replay(strategy_path, examples=examples)
        if isinstance(result, dict) and result.get('decide_source') not in (None, 'main.py:decide'):
            result['WARNING'] = (
                f"decide_source is {result.get('decide_source')!r}: the backtester could "
                f"NOT import decide() from main.py, so every number here describes the "
                f"mechanical confidence_gain-only fallback, NOT this strategy's code. "
                f"Fix the structure first: main.py's top level may contain only imports, "
                f"assignments, defs, the docstring and an `if __name__ == '__main__'` "
                f"guard, with the logic in a top-level "
                f"decide(market, history, state, config). Then re-run this tool and "
                f"check that decide_source is 'main.py:decide' before trusting any result.")
        return json.dumps(result)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def get_news_sentiment(asset: str = 'XLM', limit: int = 10) -> str:
    """Current headlines and their keyword sentiment score, as JSON."""
    try:
        import news_feed
        headlines = news_feed.get_headlines(asset=asset, limit=int(limit))
        return json.dumps({
            'asset': asset,
            'sentiment': news_feed.sentiment_score(headlines, asset=asset),
            'headline_count': len(headlines),
            'titles': [h.get('title', '') for h in headlines],
            'note': ('This is a live reading, not history. There is no headline '
                     'archive, so backtest_strategy always replays sentiment as 0.0 '
                     '(neutral) -- a news rule cannot be measured by beats_buy_hold, '
                     'only by live paper score. Read it in decide() from '
                     "state.get('news_sentiment', 0.0); never call the feed there."),
        }, indent=2)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def list_candidate_assets(limit: int = 15) -> str:
    """Ranked Stellar assets that could be added to a strategy, as JSON.

    Proposals only -- presence here is not approval. Each entry still has to pass
    verify_asset, and monitor re-checks it before the strategy starts.
    """
    try:
        from asset_discovery import discover_candidates
        return json.dumps(discover_candidates(limit=int(limit)), indent=2)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def verify_asset(code: str, issuer: str) -> str:
    """Check whether a (code, issuer) pair would be admitted, with the evidence."""
    try:
        from asset_discovery import asset_summary
        return json.dumps(asset_summary(code, issuer), indent=2)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def get_asset_price(code: str, issuer: str) -> str:
    """Current USD mark and live order-book liquidity for one Stellar asset."""
    try:
        import assets as _assets
        from dex_price import get_mark, get_orderbook
        spec = _assets.canonical(code, issuer)
        book = get_orderbook(spec) or {}
        return json.dumps({
            'spec': spec,
            'mark_usd': get_mark(spec),
            'spread_pct': book.get('spread_pct'),
            'bid_depth_usd': book.get('bid_depth_usd'),
            'ask_depth_usd': book.get('ask_depth_usd'),
        }, indent=2)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def get_asset_price_history(code: str, issuer: str, hours: int = 168) -> str:
    """Hourly OHLC history for one Stellar asset, oldest first, as JSON."""
    try:
        import assets as _assets
        from dex_price import get_candles
        spec = _assets.canonical(code, issuer)
        return json.dumps(get_candles(spec, hours=int(hours)))
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def get_dex_cex_basis(code: str = 'XLM', issuer: str = '') -> str:
    """Current DEX-vs-CEX dislocation, and whether it is worth crossing for."""
    try:
        import basis
        spec = 'XLM'
        if code and code.upper() != 'XLM':
            import assets as _assets
            spec = _assets.canonical(code, issuer or None)
        result = basis.get_basis(spec)
        if result is None:
            return json.dumps({'error': 'basis unavailable: one venue did not answer, '
                                        'or the book is wider than the sanity limit'})
        return json.dumps(result, indent=2)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def get_friction(code: str = 'XLM', issuer: str = '') -> str:
    """What one fill and one round trip in an asset actually cost, in basis points."""
    try:
        import friction
        spec = 'XLM'
        if code and code.upper() != 'XLM':
            import assets as _assets
            spec = _assets.canonical(code, issuer or None)
        return json.dumps(friction.describe(spec), indent=2)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def get_market_history(hours: int = 168) -> str:
    """Recorded per-minute market conditions: book width, depth, basis, sentiment.

    Returns a summary plus a sample of the raw rows. The summary alone is usually what a
    revision needs, and the rows can be long, so they are capped -- a model that wants
    the full series should call series() from inside main.py rather than reading it here.

    The sample is an even STRIDE across the window, not the tail. These rows were hourly
    when this tool was written; at one a minute, `rows[-200:]` answered "the last week"
    with the last three hours and nothing in the payload said so, which is precisely the
    kind of silently-narrowed window that makes a model confident about the wrong thing.
    """
    try:
        import market_recorder
        rows = market_recorder.read_history(hours=int(hours))
        stride = max(1, len(rows) // 200)
        sample = rows[::stride][-200:]
        return json.dumps({
            'summary': market_recorder.summary(hours=int(hours)),
            'span': market_recorder.span(),
            'rows': sample,
            'rows_are': (f'every {stride}th row across the whole window'
                         if stride > 1 else 'every row'),
            'total_rows': len(rows),
        }, indent=2)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def get_market_regime(hours: int = 720, buy_below: float = 0, sell_above: float = 0) -> str:
    """What market this is, and whether a specific threshold band would ever fire in it.

    Answers the question the revision model was previously guessing at. Pass your
    candidate buy_below/sell_above and `band_check` comes back with how many buys, sells
    and completed round trips they would have made over the real candles, plus a plain
    verdict -- STERILE (never bought), ONE-WAY (bought and never sold), or a round-trip
    count. Omit them and you get the regime read plus the ranked grid of widths.

    The band replay assumes the template's fixed-threshold rule, so for a strategy whose
    decide() does something cleverer it bounds the opportunity rather than predicting the
    result; backtest_strategy is what judges real logic. And suggest_bands is an in-sample
    fit -- see the `note` it returns.
    """
    try:
        import regime
        out = {'regime': regime.regime(hours=int(hours))}
        try:
            buy_below, sell_above = float(buy_below or 0), float(sell_above or 0)
        except (TypeError, ValueError):
            buy_below = sell_above = 0.0
        if buy_below > 0 and sell_above > 0:
            out['band_check'] = regime.band_stats(buy_below, sell_above, hours=int(hours))
        out['suggest_bands'] = regime.suggest_bands(hours=int(hours))
        return json.dumps(out, indent=2)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


TOOLS = {
    'get_market_regime': get_market_regime,
    'get_dex_cex_basis': get_dex_cex_basis,
    'get_friction': get_friction,
    'get_market_history': get_market_history,
    'get_news_sentiment': get_news_sentiment,
    'list_candidate_assets': list_candidate_assets,
    'verify_asset': verify_asset,
    'get_asset_price': get_asset_price,
    'get_asset_price_history': get_asset_price_history,
    'backtest_strategy': backtest_strategy,
    'backtest_forecast_strategy': backtest_forecast_strategy,
    'backtest_kalshi_strategy': backtest_kalshi_strategy,
    'get_price_history': get_price_history,
    'calculate': calculate,
    'get_current_time': get_current_time,
    'get_uptime': get_uptime,
    'read_file': read_file,
    'write_file': write_file,
    'fetch_url': fetch_url,
    'install_package': install_package,
    'update_package_list': update_package_list,
    'exec': exec,
}

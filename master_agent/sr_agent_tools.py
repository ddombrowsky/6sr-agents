import ast
import json
import operator
import shlex
import subprocess
import sys
from datetime import datetime

sys.path.append('/opt/tools')


def get_uptime() -> str:
    result = subprocess.run(['uptime'], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def read_file(path: str = '', depth=None, line_start=None, line_end=None,
              lines_start=None, lines_end=None, start=None, end=None) -> str:
    # Accept line_start/line_end and lines_start/lines_end as aliases (the model
    # uses both spellings interchangeably). Found in the wild 2026-08-23: the
    # model tried to page through a 300-line main.py by calling
    # read_file(path=..., line_start=200, line_end=400) and
    # read_file(path=..., lines_start=200, lines_end=400), both wasting a
    # tool-call round-trip on a TypeError.
    #
    # Strip whitespace: the model sometimes passes a path with a leading or
    # trailing space (found in the wild 2026-08-23: read_file(path=' /opt/...')
    # returned "No such file or directory" for a file that existed), which
    # wastes a round-trip on an opaque error.
    if path:
        path = path.strip()
    range_start = line_start if line_start is not None else (start if start is not None else lines_start)
    range_end = line_end if line_end is not None else (end if end is not None else lines_end)
    try:
        with open(path) as f:
            content = f.read()
        if range_start is not None or range_end is not None:
            lines = content.splitlines()
            try:
                s = int(range_start) - 1 if range_start is not None else 0
                e = int(range_end) if range_end is not None else len(lines)
            except (TypeError, ValueError):
                s, e = 0, len(lines)
            s = max(0, s)
            e = max(s, min(e, len(lines)))
            chunk = '\n'.join(lines[s:e])
            if s > 0:
                chunk = f'... ({s} lines before line {s + 1})\n' + chunk
            if e < len(lines):
                chunk += f'\n... ({len(lines) - e} more lines after line {e})'
            return chunk
        # Some models pass depth=N expecting the first N lines (a parameter they
        # know from other agent environments). Honour it: it saves response
        # tokens and gives the model what it asked for. Found in the wild
        # 2026-08-22: glm-5.2:cloud called read_file(path=..., depth=200) and
        # got an opaque TypeError, wasting a tool-call round-trip.
        #
        # BUT: for small files (<= 50 lines), depth is almost always a mistake
        # or a reflex -- the model called read_file(depth=1, path='config.json')
        # and got just "{" (the first line of a 5-line JSON), then had to call
        # read_file(depth=200, ...) to get the actual content (found in the wild
        # 2026-08-23 on seed_f3bae3e23122). Returning the whole file when it is
        # small costs nothing and saves a round trip. The threshold is generous:
        # main.py files run 200-400 lines and should still be paged.
        if depth is not None:
            try:
                depth = int(depth)
            except (TypeError, ValueError):
                depth = None
            if depth is not None and depth > 0:
                file_lines = content.splitlines()
                if len(file_lines) <= 50:
                    pass  # small file: return full content, ignore depth
                elif len(file_lines) > depth:
                    content = '\n'.join(file_lines[:depth]) + f'\n... ({len(file_lines) - depth} more lines)'
        return content
    except Exception as e:
        return f'error: {e}'

def write_file(path: str = '', content: str = '') -> str:
    if not path or not path.strip():
        return ('error: write_file requires a "path" argument. '
                'You provided content but no path. '
                'Call write_file(path="/path/to/file", content="...")')
    path = path.strip()
    try:
        with open(path, 'w') as f:
            f.write(content)
        return f'wrote {len(content)} bytes to {path}'
    except Exception as e:
        return f'error: {e}'


def apply_patch(patch: str = '', input: str = None, patch_text: str = None) -> str:
    """Apply a V4A ("*** Begin Patch") patch. Thin wrapper over /opt/tools/apply_patch.py.

    The parser lives in /opt/tools rather than here because there are two copies of this
    module -- /opt/agents/sr_agent_tools.py for emperor-agent.py and
    /opt/master_agent/sr_agent_tools.py for master-agent.py -- and both already have
    /opt/tools on sys.path. One implementation, no drift between the two agents.

    `input` and `patch_text` are accepted as aliases for the same reason exec() accepts
    `cmd`: the models are trained on codex's apply_patch, whose schema names the argument
    `input`, and a TypeError over the argument name costs a tool-call round trip.
    """
    text = patch or input or patch_text or ''
    if not str(text).strip():
        return ('error: no patch provided -- pass the whole patch, "*** Begin Patch" '
                'through "*** End Patch", as the `patch` argument')
    try:
        import apply_patch as patcher
    except ImportError as e:
        return (f'error: apply_patch module not available ({e}) -- use write_file '
                'with the complete file contents instead')
    try:
        return patcher.apply_patch(str(text))
    except patcher.PatchError as e:
        return f'error: {e}'
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


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
    if isinstance(node, ast.Name):
        raise ValueError(
            f'calculate does not support variables ({node.id!r}). It only '
            f'evaluates literal numeric expressions with +, -, *, /, **. '
            f'Use `exec` with Python for anything involving variables or '
            f'symbolic math.'
        )
    raise ValueError(f'unsupported expression: {ast.dump(node)}')


def calculate(expression: str) -> str:
    try:
        return str(_eval_node(ast.parse(expression, mode='eval').body))
    except Exception as e:
        return f'error: {e}'


def get_current_time() -> str:
    return datetime.now().isoformat()


def exec(command: str = '', cmd=None, timeout=None) -> str:
    # Accept cmd as an alias for command. glm-5.2:cloud repeatedly calls
    # exec(cmd=...) instead of exec(command=...) -- 5 times in one cycle
    # (2026-08-22), each wasting a tool-call round-trip even with the seventh
    # emperor pass's improved TypeError message. The model also sometimes
    # passes a list (cmd=['bash', '-lc', ...]) expecting a subprocess-style
    # call; join it into a string so /bin/sh -c can run it.
    if not command and cmd is not None:
        if isinstance(cmd, list):
            # shlex.join, not plain space-join: the model passes lists like
            # ['bash', '-lc', 'some command with spaces'], and naive space-join
            # produces 'bash -lc some command with spaces' where bash -c only
            # takes 'some' as the command. shlex.join quotes the element with
            # spaces: bash -lc 'some command with spaces'. Found in the wild
            # 2026-08-23: clone_ae4a117bcfdd wasted ~10 tool calls on broken
            # exec(cmd=[...]) invocations before working around it.
            command = shlex.join(str(c) for c in cmd)
        else:
            command = str(cmd)
    if not command:
        return 'error: no command provided'
    # Accept a timeout parameter. Found in the wild 2026-08-23: the model
    # called exec(cmd=[...], timeout=120000) and got a TypeError, wasting a
    # round-trip. The model likely means milliseconds (120000 ms = 120 s), so
    # values above 1000 are divided by 1000. Capped at 600s for safety.
    exec_timeout = 120
    if timeout is not None:
        try:
            t = int(timeout)
            if t > 1000:
                t = t // 1000  # treat as milliseconds
            exec_timeout = max(1, min(t, 600))
        except (TypeError, ValueError):
            pass
    try:
        result = subprocess.run(
            ['/bin/sh', '-c', command],
            capture_output=True, text=True, timeout=exec_timeout,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            output += f'\n[exit code {result.returncode}]'
        return output
    except subprocess.TimeoutExpired:
        return f'error: timed out after {exec_timeout}s'
    except Exception as e:
        return f'error: {e}'

def search(path: str = '/opt', query: str = '', pattern: str = '', max_results: int = 20) -> str:
    """Search for a text pattern in files under a directory (grep -rn).

    Found in the wild 2026-08-23: the revision model tried calling a 'search'
    tool that did not exist, wasting tool-call round-trips. This provides the
    grep-based file content search the model was looking for. `pattern` is
    accepted as an alias for `query` because the model uses both spellings
    (some agent environments name the parameter `pattern`, others `query`).
    """
    search_query = query or pattern
    if not search_query:
        return 'error: no query provided (pass query= or pattern=)'
    if not path:
        path = '/opt'
    try:
        result = subprocess.run(
            ['grep', '-rn',
             '--include=*.py', '--include=*.json', '--include=*.md',
             '--include=*.txt', '--include=*.sh', '--include=*.cfg',
             search_query, path],
            capture_output=True, text=True, timeout=30)
        output = result.stdout
        if not output.strip():
            return f'no matches for {search_query!r} in {path}'
        lines = output.strip().splitlines()
        if len(lines) > max_results:
            shown = lines[:max_results]
            return '\n'.join(shown) + f'\n... ({len(lines) - max_results} more matches)'
        return '\n'.join(lines)
    except subprocess.TimeoutExpired:
        return 'error: search timed out after 30s'
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
        result = backtest(strategy_path, days=float(days),
                          ticks_per_candle=int(ticks_per_candle),
                          interval=int(interval))
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


def backtest_yield_strategy(strategy_path: str) -> str:
    """Replay a yield-allocation strategy over the recorded venue rate history.

    The yield-domain analogue of backtest_strategy. Two things to read carefully:

    `trades` counts ALLOCATION DECISIONS INCLUDING THE FIRST, not rotations -- in this
    domain allocating once and never moving again is the null, not a broken strategy, and
    a 0 here means choose() never put the money anywhere at all.

    `excess_bp` is the number that matters: annualized basis points against the null,
    which is what the strategy is scored on. A large negative excess with a high
    `rotations` count means the strategy is paying for moves that do not pay for
    themselves, which is by far the commonest way to lose here.

    Imported lazily for the same reason backtest_strategy is: a broken or missing
    /opt/tools module must never stop the agent from starting.
    """
    try:
        from yield_backtest import replay
        result = replay(strategy_path)
        if result is None:
            return json.dumps({
                'result': None,
                'reason': ('not enough recorded venue history yet -- the recorder needs '
                           'several hours before a replay means anything. This is normal '
                           'on a freshly created container and is not a fault in your '
                           'strategy.')})
        source = (result.get('raw') or {}).get('source')
        if source != 'main.py:choose':
            result['WARNING'] = (
                f"source is {source!r}: the backtester could NOT import choose() from "
                f"main.py, so every number here describes the mechanical config-only "
                f"fallback, NOT this strategy's code. Fix the structure first: main.py's "
                f"top level may contain only imports, assignments, defs, the docstring "
                f"and an `if __name__ == '__main__'` guard -- even a bare "
                f"sys.path.append() up there fails it -- with the logic in a top-level "
                f"choose(rows, current, state, config, now). Then re-run this tool and "
                f"check that source is 'main.py:choose' before trusting any result.")
        return json.dumps(result)
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def backtest_maker_strategy(strategy_path: str, days: float = 7) -> str:
    """Replay a market-making strategy over recorded order book and executed tape.

    The maker-domain analogue of backtest_strategy. Read `spread_captured_usd` and
    `adverse_selection_usd` TOGETHER -- gross spread capture alone makes every width look
    profitable, and `net_edge_usd` is their difference and what beats_null is decided on.
    `trades` counts FILLS, so a strategy that quotes diligently at a price the tape never
    crosses reports zero here, which is exactly the failure this tool exists to surface.

    Imported lazily for the same reason backtest_strategy is: a broken or missing
    /opt/tools module must never stop the agent from starting.
    """
    try:
        from maker_backtest import replay
        result = replay(strategy_path, days=float(days))
        if isinstance(result, dict) and result.get('decide_source') not in (None, 'main.py:quote'):
            result['WARNING'] = (
                f"decide_source is {result.get('decide_source')!r}: the backtester could "
                f"NOT import quote() from main.py, so every number here describes the "
                f"mechanical config-genome fallback, NOT this strategy's code. Fix the "
                f"structure first: main.py's top level may contain only imports, "
                f"assignments, defs, the docstring and an `if __name__ == '__main__'` "
                f"guard, with the logic in a top-level quote(book, state, config). Then "
                f"re-run this tool and check that decide_source is 'main.py:quote' "
                f"before trusting any result.")
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


def get_tape_stats(hours: int = 24) -> str:
    """Executed-trade statistics for XLM/USDC by size bucket, as JSON.

    What a maker needs for sizing and cannot get from the order book: how much actually
    trades per hour at or above the size you would quote, how one-sided the aggressing
    flow has been, and what share of volume was liquidity-pool flow that no resting offer
    could have captured.
    """
    try:
        import dex_trades
        return json.dumps(dex_trades.tape_stats(hours=int(hours)), indent=2)
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
    'backtest_strategy': backtest_strategy,
    'backtest_forecast_strategy': backtest_forecast_strategy,
    'backtest_yield_strategy': backtest_yield_strategy,
    'backtest_maker_strategy': backtest_maker_strategy,
    'get_tape_stats': get_tape_stats,
    'backtest_kalshi_strategy': backtest_kalshi_strategy,
    'get_price_history': get_price_history,
    'calculate': calculate,
    'get_current_time': get_current_time,
    'get_uptime': get_uptime,
    'read_file': read_file,
    'write_file': write_file,
    'apply_patch': apply_patch,
    'fetch_url': fetch_url,
    'install_package': install_package,
    'update_package_list': update_package_list,
    'exec': exec,
    'search': search,
}

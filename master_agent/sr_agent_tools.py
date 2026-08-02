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


def backtest_strategy(strategy_path: str, days: int = 30, ticks_per_candle: int = 1) -> str:
    """Replay a strategy over real historical candles and return a JSON result summary.

    Imported lazily so a broken/missing /opt/tools module can never stop the agent from
    starting -- the same reason the price feed is imported inside monitor.py's helper.
    """
    try:
        from backtest import backtest
        # legs=True costs nothing for an XLM-only strategy (no declared assets means no
        # extra work) and is what makes a multi-asset revision reviewable at all.
        return json.dumps(backtest(strategy_path, days=int(days),
                                   ticks_per_candle=int(ticks_per_candle), legs=True))
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


def get_price_history(hours: int = 720, interval: int = 60) -> str:
    """Recent XLM/USD close prices as a JSON list, oldest first."""
    try:
        from ohlc_history import closes
        return json.dumps(closes(hours=int(hours), interval=int(interval)))
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


TOOLS = {
    'list_candidate_assets': list_candidate_assets,
    'verify_asset': verify_asset,
    'get_asset_price': get_asset_price,
    'get_asset_price_history': get_asset_price_history,
    'backtest_strategy': backtest_strategy,
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

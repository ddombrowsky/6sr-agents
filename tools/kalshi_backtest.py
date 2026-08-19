#!/usr/bin/env python3
"""Replay a Kalshi strategy against a fixed set of already-resolved markets.

The Kalshi-domain analogue of forecast_backtest.py, which is itself the analogue of
tools/backtest.py: without this, the only way to evaluate a revision is to start it and
wait real days for real markets to settle -- exactly the feedback-latency problem
KALSHI.md's "core design problem" section describes, and the reason domain.py's contract
requires a cheap offline replay at all (criterion 2).

FIXED SET, BUT NOT SEEDED -- confirmed live 2026-08-10/11 per KALSHI.md's Phase 0 answer
=============================================================================================
forecast_backtest.py's set is a pure function of BASELINE_SEED: infinite, free, and
exactly reproducible forever. Kalshi has no equivalent generative model -- there is no
seed that produces "a real Chicago temperature market," only the real exchange's own
history. So the set here is built by actually crawling already-settled markets
(kalshi_api.list_resolved_markets) and caching the result to BACKTEST_SET_PATH with a
TTL (SET_TTL): "fixed" means "the same set for a day at a time, so two backtests run
minutes apart are comparable," not "the same set forever." Rebuilding needs network
access; a candidate whose replay runs while the cache is warm needs none, same as any
other call in this module failing open rather than raising.

WHY THE AS-OF READING IS AN EARLY CANDLE, NOT THE SETTLEMENT PRICE
=======================================================================
A resolved market's own last_price_dollars has usually already converged close to 0 or 1
by the time it closes (see kalshi_api.py's module docstring: KXHIGHCHI-26AUG09-T90 was at
0.01 well before its actual close). Backtesting decide() against that price would be
almost content-free -- the market has essentially already told you the answer, so
"parrot the current price" scores near-perfectly for a reason that has nothing to do with
skill. Each example here instead uses a candle from roughly a QUARTER of the way through
the market's trading window (see _build_example) as the as-of implied_prob, with a short
run of candles immediately before it as `history` -- genuinely uncertain, and with real
momentum for the template's default decide() to react to.

IMPORTABILITY, same rule as forecast_backtest.py
====================================================
A candidate main.py is only replayed if its module top level is nothing but imports,
assignments, defs, the docstring and an `if __name__` guard, and it defines a top-level
`decide(market, history, state, config) -> p_hat`. Importing a module runs its top level,
and the live template's top level runs a tick loop forever -- so anything looser than
this would hang the backtest. Independently implemented (not imported from
forecast_backtest.py or backtest.py) so this module has no cross-domain import
dependency, same reasoning forecast_backtest.py's own docstring gives.

CLI:
    python3 /opt/tools/kalshi_backtest.py /opt/strategies/<name> [--rebuild]
"""
import ast
import importlib.util
import json
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import kalshi_api

BACKTEST_SET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  '.kalshi_backtest_set.json')
SET_TTL = 24 * 3600     # see module docstring's FIXED SET section
N_MARKETS = 300
SERIES_PER_CATEGORY = 40
CANDLES_BEFORE = 5      # candle closes of history handed to decide() before the as-of point
DEFAULT_CATEGORY = 'Climate and Weather'
DEFAULT_FREQUENCY = 'daily'

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
    """(ok, reason) for whether this can import decide() from `main_py`. Accepts a
    path, a Path, or the source text itself."""
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
    return importability_report(main_py)[0]


def _default_decide(config):
    """The template's own rule, used when main.py can't be imported at all."""
    gain = float(config.get('confidence_gain', 1.0))

    def decide(market, history, state, cfg):
        p = market.get('implied_prob')
        if p is None:
            return 0.5
        drift = (history[-1] - history[0]) if len(history) >= 2 else 0.0
        return min(1.0, max(0.0, p + gain * drift))

    return decide


def _load_decide(strategy_dir, config):
    main_py = os.path.join(strategy_dir, 'main.py')
    if not os.path.exists(main_py) or not _is_importable(main_py):
        return _default_decide(config), 'config-thresholds'
    try:
        spec = importlib.util.spec_from_file_location(
            f'_kbt_{os.path.basename(strategy_dir)}', main_py)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.decide, 'main.py:decide'
    except Exception as e:
        print(f'[kalshi_backtest] could not import decide from {main_py}: {e}')
        return _default_decide(config), 'config-thresholds'


def _build_example(market):
    """One backtest example from a resolved `market`, or None if there isn't enough
    candle history to build a genuinely pre-resolution reading. See module docstring's
    WHY THE AS-OF READING IS AN EARLY CANDLE section."""
    ticker = market.get('ticker')
    series = market.get('series_ticker')
    open_t, close_t = market.get('open_time'), market.get('close_time')
    result = market.get('result')
    if not (ticker and series and open_t and close_t and result in ('yes', 'no')):
        return None
    if close_t <= open_t:
        return None
    candles = kalshi_api.get_candlesticks(series, ticker, int(open_t), int(close_t),
                                          period_interval=60)
    candles = [c for c in candles if c.get('close') is not None and c.get('volume')]
    if len(candles) < CANDLES_BEFORE + 2:
        return None
    # Roughly a quarter of the way through the traded window: early enough the price
    # usually hasn't converged, late enough CANDLES_BEFORE real readings exist first.
    idx = min(max(CANDLES_BEFORE, len(candles) // 4), len(candles) - 1)
    history = [c['close'] for c in candles[idx - CANDLES_BEFORE:idx]]
    as_of = candles[idx]
    return {
        'market': {'ticker': ticker, 'implied_prob': as_of['close'],
                   'volume': as_of.get('volume'),
                   'open_interest': market.get('open_interest'),
                   'close_time': market.get('close_time')},
        'history': history,
        'outcome': 1.0 if result == 'yes' else 0.0,
    }


def build_backtest_set(force=False):
    """[{'market', 'history', 'outcome'}, ...], cached to BACKTEST_SET_PATH for
    SET_TTL. Rebuilds by crawling kalshi_api.list_resolved_markets on a cache miss or
    when `force` -- see module docstring's FIXED SET section for what "fixed" means
    here. Never raises; a crawl failure just yields whatever was already cached, or []."""
    if not force and os.path.exists(BACKTEST_SET_PATH):
        try:
            with open(BACKTEST_SET_PATH) as f:
                data = json.load(f)
            if time.time() - data.get('built_at', 0) < SET_TTL and data.get('examples'):
                return data['examples']
        except Exception:
            pass
    examples = []
    try:
        resolved = kalshi_api.list_resolved_markets(
            category=DEFAULT_CATEGORY, frequency=DEFAULT_FREQUENCY,
            max_series=SERIES_PER_CATEGORY, limit_per_series=20)
    except Exception as e:
        print(f'[kalshi_backtest] could not crawl resolved markets: {e}')
        resolved = []
    for m in resolved:
        ex = _build_example(m)
        if ex:
            examples.append(ex)
        if len(examples) >= N_MARKETS:
            break
    if examples:
        try:
            with open(BACKTEST_SET_PATH, 'w') as f:
                json.dump({'built_at': time.time(), 'examples': examples}, f)
        except Exception:
            pass
    return examples


def replay(strategy_dir, examples=None):
    """Replay `strategy_dir` over the fixed resolved-market set.

    Returns {'trades', 'beats_null', 'null_pct', 'mean_brier', 'decide_source'} -- the
    same shape domain_sdex.replay()/domain_forecast.replay() return, since all three
    feed the same domain.py contract member. `null_pct` here is the mean Brier the
    market's OWN frozen-at-as-of price would have scored against the real outcome --
    "beats_null" answers "would this decide() have beaten just trading at the market's
    own price," which is FUTURE.md's stated null for this domain.
    """
    if examples is None:
        examples = build_backtest_set()
    if not examples:
        return {'trades': 0, 'beats_null': None, 'null_pct': None, 'mean_brier': None,
                'decide_source': None, 'error': 'no backtest examples available '
                '(crawl failed or nothing resolved yet)'}

    config_path = os.path.join(strategy_dir, 'config.json')
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception as e:
            print(f'[kalshi_backtest] could not read {config_path}: {e}')

    decide, source = _load_decide(strategy_dir, config)

    brier_sum = market_brier_sum = 0.0
    trades = 0
    state = {}
    for ex in examples:
        try:
            p_hat = decide(ex['market'], ex['history'], state, config)
        except Exception as e:
            if trades == 0:
                return {'error': f'decide() raised {type(e).__name__}: {e}',
                        'decide_source': source}
            break
        try:
            p_hat = float(p_hat)
        except (TypeError, ValueError):
            break
        if p_hat != p_hat:  # NaN
            break
        p_hat = min(1.0, max(0.0, p_hat))
        outcome = ex['outcome']
        brier_sum += (p_hat - outcome) ** 2
        market_brier_sum += (ex['market']['implied_prob'] - outcome) ** 2
        trades += 1

    if trades == 0:
        return {'trades': 0, 'beats_null': None, 'null_pct': None,
                'decide_source': source, 'mean_brier': None}

    mean_brier = brier_sum / trades
    mean_market_brier = market_brier_sum / trades
    return {
        'trades': trades,
        'decide_source': source,
        'mean_brier': mean_brier,
        'mean_market_brier': mean_market_brier,
        'beats_null': mean_brier < mean_market_brier,
        'null_pct': mean_market_brier,
    }


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1])
        sys.exit(1)
    path = sys.argv[1]
    if '--rebuild' in sys.argv:
        examples = build_backtest_set(force=True)
        print(f'rebuilt: {len(examples)} example(s)')
    else:
        examples = None
    print(json.dumps(replay(path, examples=examples), indent=2))

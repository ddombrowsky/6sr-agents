#!/usr/bin/env python3
"""Replay a forecasting strategy against a fixed set of resolved questions.

The forecast-domain analogue of tools/backtest.py: until this exists, the only way to
evaluate a revision is to start it and wait for real ticks to resolve -- exactly the
"one extremely noisy sample per cycle" problem backtest.py's own docstring describes for
sdex, and the reason domain.py's contract requires a cheap offline replay at all
(criterion 2). This one runs against a few thousand pre-generated, already-resolved
questions in a fraction of a second, with no network and no clock dependency.

FIXED SET, NOT THE LIVE GAME
=============================
The historical set here is generated from BASELINE_SEED, a different constant than
forecast_engine.LIVE_SEED. Same generative distribution (both call
forecast_engine.question_at, so a backtest measures the same skill the live game
does), but a different seed means: (a) the set is exactly the same every time this is
run, regardless of how many live ticks have actually elapsed, so two backtests of the
same main.py are always comparable to each other; (b) a strategy cannot gain anything by
having "seen" these particular questions live, because they never appear in the live
game at all.

IMPORTABILITY, same rule as backtest.py
=========================================
A candidate main.py is only replayed if its module top level is nothing but imports,
assignments, defs, the docstring and an `if __name__` guard, and it defines a top-level
`decide(feature, history, state, config) -> p_hat`. Importing a module runs its top
level, and the live template's top level runs a tick loop forever -- so anything looser
than this would hang the backtest. See backtest.py's own docstring for the incident that
established this rule; the logic here is the same rule, independently implemented so this
module has no import-time dependency on backtest.py's sdex-specific pieces.

CLI:
    python3 /opt/tools/forecast_backtest.py /opt/strategies/<name> [n]
"""
import ast
import importlib.util
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import forecast_engine

BASELINE_SEED = 'sr-agents-forecast-baseline-v1'
N_QUESTIONS = 2000

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
    """(ok, reason) for whether this can import decide() from `main_py`.

    Accepts a path, a Path, or the source text itself, mirroring backtest.py's version so
    a candidate pulled from `git show` doesn't have to be written to disk first.
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
    return importability_report(main_py)[0]


def _default_decide(config):
    """The template's own rule, used when main.py can't be imported at all -- so a
    completely broken revision still gets a (bad) number instead of an error."""
    gain = float(config.get('confidence_gain', 1.0))

    def decide(feature, history, state, cfg):
        return min(1.0, max(0.0, 0.5 + gain * (feature - 0.5)))

    return decide


def _load_decide(strategy_dir, config):
    main_py = os.path.join(strategy_dir, 'main.py')
    if not os.path.exists(main_py) or not _is_importable(main_py):
        return _default_decide(config), 'config-thresholds'
    try:
        spec = importlib.util.spec_from_file_location(
            f'_fbt_{os.path.basename(strategy_dir)}', main_py)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.decide, 'main.py:decide'
    except Exception as e:
        print(f'[forecast_backtest] could not import decide from {main_py}: {e}')
        return _default_decide(config), 'config-thresholds'


def replay(strategy_dir, n=N_QUESTIONS):
    """Replay `strategy_dir` over `n` fixed, already-resolved questions.

    Returns {'trades', 'beats_null', 'null_pct', 'raw'} -- the same shape
    domain_sdex.replay() returns, since both feed the same domain.py contract member.
    `trades` is the number of forecasts actually scored (a decide() that raises partway
    through still returns whatever it scored up to that point, rather than an error, so a
    strategy that mostly works isn't reverted over one bad question -- consistent with
    replay() being a fitness signal the loop fails open on, not a hard gate).
    """
    config_path = os.path.join(strategy_dir, 'config.json')
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception as e:
            print(f'[forecast_backtest] could not read {config_path}: {e}')

    decide, source = _load_decide(strategy_dir, config)

    history = []
    brier_sum = base_sum = 0.0
    trades = 0
    state = {}
    for i in range(int(n)):
        p_true, feature, outcome = forecast_engine.question_at(0, i, seed=BASELINE_SEED)
        history.append(feature)
        try:
            p_hat = decide(feature, history, state, config)
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
        brier_sum += (p_hat - outcome) ** 2
        base_sum += forecast_engine.BASE_BRIER
        trades += 1

    if trades == 0:
        return {'trades': 0, 'beats_null': None, 'null_pct': None,
                'decide_source': source, 'mean_brier': None}

    mean_brier = brier_sum / trades
    mean_base = base_sum / trades
    return {
        'trades': trades,
        'decide_source': source,
        'mean_brier': mean_brier,
        'mean_base_brier': mean_base,
        # Lower Brier is better, hence the flipped comparison. Named beats_null/null_pct
        # directly (not sdex's beats_buy_hold/buy_hold_pct) since domain_forecast.py
        # reads these keys explicitly rather than generically -- no need to borrow
        # sdex's naming here.
        'beats_null': mean_brier < mean_base,
        'null_pct': mean_base,
    }


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1])
        sys.exit(1)
    path = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else N_QUESTIONS
    print(json.dumps(replay(path, n=n), indent=2))

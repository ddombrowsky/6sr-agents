#!/usr/bin/env python3
"""Replay a candidate strategy's own choose() over recorded venue history.

This is the offline replay domain.py's criterion 2 asks for, and until it existed
`domain_yield.replay()` returned None -- so a revision was accepted if it parsed and ran,
and nothing checked whether it did anything sensible. It is the sibling of
tools/yield_replay.py and the distinction between them is worth keeping straight:

    yield_replay.py   scores decisions that were ACTUALLY MADE, from the activity log
    yield_backtest.py runs a candidate's code over history it never lived through

Both price the result with the same engine, so a backtest and a score cannot disagree
about what a rotation costs or what a venue paid.

WHAT `trades` MEANS HERE, AND WHY IT IS NOT ROTATIONS. monitor.py treats a revision that
replays zero trades as unrevised and throws it away -- the gate exists because the model
has repeatedly shipped strategies that could never act while quoting a backtest as
justification. In a trading domain "acted" means "traded". In THIS domain the correct
behaviour is frequently to hold: a strategy that allocates once to the best venue and
never moves again is not sterile, it is the null, and counting only rotations would
revert it for being right. So `trades` counts ALLOCATION DECISIONS INCLUDING THE FIRST.
Zero means choose() never put the money anywhere, which is the sterile case the gate is
actually for.

The genome is imported, not reimplemented: `choose(rows, current, state, config, now)`
is called with the same row dicts a live tick would see. A main.py whose module top level
does anything but define things is refused by importability_report before it is imported,
which is the same AST rule tools/forecast_backtest.py applies -- see the template's
"Structure matters here" docstring, which now has a checker behind it.
"""
import ast
import importlib.util
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yield_recorder
import yield_replay

# Below this much history a replay is not evidence. Rotation decisions in this domain are
# separated by hours, so a window of minutes cannot contain one and every candidate would
# come back looking identical -- which reads as "no difference between these strategies"
# when it means "no measurement".
MIN_HISTORY_S = 6 * 3600

# A backtest runs inside monitor's revision path, which is already the slowest thing in a
# cycle. An LLM-written choose() that is accidentally quadratic must cost one gate, not
# the window.
TIME_BUDGET_S = 30

_TOP_LEVEL_RULE = ('the module top level must only import, define and assign -- a tick '
                   "loop outside `if __name__ == '__main__':` runs on import")


def _source_of(main_py):
    """main.py's text, whether given as a path, a Path, or the source itself."""
    if hasattr(main_py, 'read_text'):
        return main_py.read_text()
    if isinstance(main_py, str) and not os.path.exists(main_py):
        return main_py
    return open(main_py).read()


def _describe(node):
    try:
        text = ast.unparse(node).splitlines()[0]
    except Exception:
        text = ''
    if len(text) > 60:
        text = text[:57] + '...'
    return (f'{type(node).__name__} at line {getattr(node, "lineno", "?")}'
            + (f': {text}' if text else ''))


def importability_report(main_py):
    """(ok, reason) for whether choose() can be imported out of `main_py`.

    Accepts a path, a Path, or the source itself, so a candidate pulled from `git show`
    does not have to be written to disk first.
    """
    try:
        tree = ast.parse(_source_of(main_py))
    except SyntaxError as e:
        return False, f'main.py has a syntax error: {e.msg} (line {e.lineno})'
    except Exception as e:
        return False, f'main.py could not be read: {e}'

    has_choose = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == 'choose':
                has_choose = True
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.Assign,
                             ast.AnnAssign, ast.Pass)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue        # docstring
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name) and test.left.id == '__name__'):
                continue    # __main__ guard, not executed on import
        return False, f'top-level {_describe(node)} -- {_TOP_LEVEL_RULE}'
    if not has_choose:
        return False, 'no top-level choose() function'
    return True, 'choose() is importable'


def _default_choose(config):
    """The template's rule, used when main.py cannot be imported at all.

    A completely broken revision still gets a number rather than an error, for the same
    reason forecast_backtest keeps one: the caller fails open on None, so returning None
    for a strategy that is merely unimportable would hide the very thing worth reporting.
    """
    def choose(rows, current, state, config, now):
        weight = float(config.get('emission_weight', 0.5))
        floor = float(config.get('min_free_liquidity_usd', 0.0))
        eligible = [r for r in rows
                    if r['utilization'] < r['max_utilization']
                    and (floor <= 0 or (r['free_liquidity_usd'] or 0.0) >= floor)]
        if not eligible:
            return current
        count = max(1, int(config.get('max_venues', 1)))
        ranked = sorted(eligible,
                        key=lambda r: -(r['supply_apy'] + weight * r['emission_apr_gross']))
        target = ranked[:count]
        if not current:
            return target
        held_at = float(state.get('last_rotation_ts', 0.0))
        if now - held_at < float(config.get('rebalance_hours', 12.0)) * 3600:
            return current
        def score(rows_):
            return sum(r['supply_apy'] + weight * r['emission_apr_gross']
                       for r in rows_) / len(rows_)
        if (score(target) - score(current)) * 10000 < float(config.get('min_edge_bp', 50.0)):
            return current
        return target
    return choose


def _load_choose(strategy_dir, config):
    main_py = os.path.join(strategy_dir, 'main.py')
    if not os.path.exists(main_py):
        return _default_choose(config), 'config-only (no main.py)'
    ok, _reason = importability_report(main_py)
    if not ok:
        return _default_choose(config), 'config-only (main.py not importable)'
    try:
        spec = importlib.util.spec_from_file_location(
            f'_ybt_{os.path.basename(strategy_dir)}', main_py)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.choose, 'main.py:choose'
    except Exception as e:
        print(f'[yield_backtest] could not import choose from {main_py}: {e}')
        return _default_choose(config), 'config-only (import raised)'


def _key(row):
    return (row['pool_address'], row['asset_address'])


def simulate(choose, config, history):
    """Drive `choose` over every sample and return its intended-allocation timeline.

    The state dict handed to choose() carries the two things the template's own rule
    reads back -- `last_rotation_ts` and `allocation` -- so a candidate that reasons about
    how long it has held sees the same thing it would live. It is deliberately NOT the
    real state.json: nothing a strategy writes is an input to its own evaluation.
    """
    changes = []
    held = []
    state = {'last_rotation_ts': 0.0, 'allocation': [], 'nav': 1.0, 'rotations': 0}
    started = time.time()
    for row in history:
        if time.time() - started > TIME_BUDGET_S:
            return changes, 'timeout'
        ts = float(row['ts'])
        rows = row.get('rows') or []
        by_key = {_key(r): r for r in rows}
        current = [by_key[k] for k in held if k in by_key]
        try:
            target = choose(rows, current, state, config, ts)
        except Exception:
            continue        # a raising choose() holds, exactly as main.py's tick does
        if not isinstance(target, list):
            continue
        keys = [_key(r) for r in target if isinstance(r, dict) and _key(r) in by_key]
        if set(keys) == set(held):
            continue
        held = keys
        weight = 1.0 / len(keys) if keys else 0.0
        changes.append((ts, {k: weight for k in keys}))
        state['last_rotation_ts'] = ts
        state['rotations'] += 1
        state['allocation'] = [{'pool': by_key[k]['pool'], 'pool_address': k[0],
                                'asset': by_key[k]['asset'], 'asset_address': k[1],
                                'weight': weight} for k in keys]
    return changes, 'ok'


def replay(strategy_dir):
    """{'trades', 'beats_null', 'null_pct', 'raw'} or None if it cannot be measured.

    None means "could not be measured" -- the contract's value that every caller in the
    loop fails open on. It is returned when there is not yet enough recorded history,
    which on a fresh container is the normal state for the first several hours.
    """
    try:
        config = json.load(open(os.path.join(strategy_dir, 'config.json')))
    except Exception:
        return None

    history = yield_recorder.history()
    if len(history) < 2:
        return None
    since, until = float(history[0]['ts']), float(history[-1]['ts'])
    if until - since < MIN_HISTORY_S:
        return None

    choose, source = _load_choose(strategy_dir, config)
    changes, status = simulate(choose, config, history)
    mine = yield_replay.run(history, changes, since, until)
    null = yield_replay.null_static_best(history, since, until)
    if mine is None or null is None:
        return None

    years = yield_replay.SECONDS_PER_YEAR / max(1.0, mine['covered_s'])
    excess_bp = (mine['return'] - null['return']) * years * 10000.0
    return {
        # Allocation decisions INCLUDING the first -- see this module's docstring. Zero
        # means the money never went anywhere, which is what monitor's gate is for.
        'trades': len(changes),
        'beats_null': mine['return'] > null['return'],
        'null_pct': round(null['return'] * years * 100.0, 4),
        'raw': {
            'source': source,
            'status': status,
            'hours': round((until - since) / 3600.0, 2),
            'strategy_apy_pct': round(mine['return'] * years * 100.0, 4),
            'excess_bp': round(excess_bp, 1),
            'rotations': mine['rotations'],
            'cost_bp': mine['cost_bp'],
        },
    }


if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else '.'
    if '--importability' in sys.argv:
        print(json.dumps(importability_report(os.path.join(target, 'main.py'))))
    else:
        print(json.dumps(replay(target), indent=2))

#!/usr/bin/env python3
"""Did this strategy's real fills happen at better venue moments than luck would give?

The live-log counterpart to backtest.py's basis metrics, and the reason both exist:
`beats_buy_hold` is a directional measure, and mining the DEX/CEX basis is not a
directional claim. A basis strategy asserts that it *times* its fills against the gap
between where XLM is quoted (the CEX aggregate every strategy decides on) and where this
system actually trades it (the Stellar DEX book). Nothing in score.py or the leaderboard
can see that claim, because both look only at net worth.

The statistic, per fill:

    edge_bp = -basis_bp on a buy      (DEX cheap against the CEX is good for a buyer)
    edge_bp = +basis_bp on a sell

and the null it is measured against is the same statistic over every recorded moment in
the window, weighted by this strategy's own realized buy/sell mix -- a basis-blind
strategy's fill times are uncorrelated with the basis, so that is what it gets in
expectation. `basis_edge_excess_bp` is the difference, and it is the whole claim.

The arithmetic itself lives in tools/backtest.py (`basis_edge_stats`) and is imported
here rather than reimplemented, so the replayed number and the live number cannot drift
into being two different statistics sharing one name -- which is exactly what happened to
basis_bp/tradeable_bp when market_recorder recomputed them beside basis.py.

WHY THIS IS IN master_agent/ AND NOT tools/: same reason as live_report.py. /opt/tools is
the watched money boundary, and a reporting script there costs a live-trading halt and a
manual re-baseline every time its output format is tweaked. Nothing here writes anything,
touches live.flag, or imports stellar_trader -- it is a post-hoc join of two files that
were already being written.

CLI:
    python3 /opt/master_agent/basis_report.py [hours]
"""
import json
import sys
from pathlib import Path

if '/opt/tools' not in sys.path:
    sys.path.append('/opt/tools')

TRADES_DIR = Path('/opt/trades')
STRATEGIES_DIR = Path('/opt/strategies')

# A fill is matched to the last recorded row at or before it, within this many seconds.
# Same rule and the same reason as backtest._basis_join: never the nearest row, because
# a row from after the fill is information the strategy did not have. Five minutes is
# five recorder intervals.
MATCH_TOLERANCE_S = 300


def _series(hours):
    """Recorded (ts, basis_bp) for XLM, oldest first. [] if unavailable."""
    try:
        import market_recorder
        rows = market_recorder.read_history(hours=hours, spec='XLM')
    except Exception:
        return []
    out = []
    for row in rows:
        try:
            if row.get('ts') and row.get('basis_bp') is not None:
                out.append((float(row['ts']), float(row['basis_bp'])))
        except Exception:
            continue
    out.sort(key=lambda r: r[0])
    return out


def _basis_at(series, ts):
    """The basis in force at `ts`: last row at or before it, within tolerance."""
    lo, hi = 0, len(series) - 1
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts:
            found = series[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    if found is None or ts - found[0] > MATCH_TOLERANCE_S:
        return None
    return found[1]


def _fills(name, hours):
    """(ts, side) for this strategy's XLM paper fills in the window, oldest first.

    XLM only. The basis series is XLM's, and an extra leg's fill timed against XLM's
    dislocation would be a meaningless pairing -- worse than no number, because it would
    average into the same column.
    """
    log = TRADES_DIR / f'{name}.log'
    if not log.exists():
        return []
    import time
    cutoff = time.time() - float(hours) * 3600
    out = []
    try:
        with log.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue    # a torn last line is normal; skip it, don't raise
                ts = row.get('timestamp')
                if not ts or ts < cutoff:
                    continue
                if row.get('asset_spec', 'XLM') != 'XLM':
                    continue
                action = row.get('action')
                if action in ('buy', 'sell'):
                    out.append((float(ts), action))
    except Exception:
        pass
    return out


def edge_for(name, hours=24, series=None):
    """The basis edge for one strategy over the last `hours`. Never raises."""
    try:
        from backtest import basis_edge_stats
    except Exception as e:
        return {'name': name, 'error': f'basis metrics unavailable: {e}'}

    if series is None:
        series = _series(hours)
    fills = _fills(name, hours)

    edges, buys, sells = [], 0, 0
    matched = 0
    for ts, side in fills:
        basis_bp = _basis_at(series, ts)
        if basis_bp is None:
            continue
        matched += 1
        if side == 'buy':
            edges.append(-basis_bp)
            buys += 1
        else:
            edges.append(basis_bp)
            sells += 1

    population = sum(b for _, b in series) / len(series) if series else None
    coverage = round(matched / len(fills), 4) if fills else 0.0
    out = {
        'name': name,
        'trades': len(fills),
        'matched': matched,
        'coverage': coverage,
        'population_basis_bp': round(population, 3) if population is not None else None,
        'gated': _has_basis_knob(name),
    }
    out.update(basis_edge_stats(edges, buys, sells, population, coverage=coverage))
    return out


def _has_basis_knob(name):
    """Is this strategy in the basis-gated arm? i.e. does its config carry the knob.

    The A/B split monitor._inject_basis_gate creates. Read from config.json rather than
    from main.py, because the knob is what the injection sets and what apply_random_tweak
    carries forward down a lineage.
    """
    try:
        cfg = json.loads((STRATEGIES_DIR / name / 'config.json').read_text())
        return cfg.get('basis_min_bp') is not None
    except Exception:
        return False


def _tracked_names():
    try:
        state = json.loads(Path('/opt/strategy_state.json').read_text())
        return sorted(state.keys())
    except Exception:
        return sorted(p.name for p in STRATEGIES_DIR.glob('*') if p.is_dir())


def population_edge(hours=24, names=None):
    """Per-strategy rows plus the gated-vs-control aggregate.

    That split is the readout this whole exercise exists to produce: the gate is seeded
    onto a coin-flipped half of template spawns precisely so there is something to
    compare against, and comparing them on net worth alone would take weeks of noise to
    say anything. Comparing them on realized venue edge takes one fill at a time.
    """
    series = _series(hours)
    rows = []
    for name in (names if names is not None else _tracked_names()):
        row = edge_for(name, hours, series=series)
        if row.get('basis_edge_n'):
            rows.append(row)
    rows.sort(key=lambda r: (r.get('basis_edge_excess_bp') is None,
                             -(r.get('basis_edge_excess_bp') or 0)))

    def _arm(gated):
        sel = [r for r in rows if r['gated'] is gated]
        n = sum(r['basis_edge_n'] for r in sel)
        if not n:
            return {'strategies': len(sel), 'fills': 0, 'excess_bp': None}
        # Fill-weighted, not strategy-weighted: a strategy with 400 fills is 400
        # observations of the same claim and a strategy with 11 is 11.
        weighted = sum((r['basis_edge_excess_bp'] or 0) * r['basis_edge_n'] for r in sel)
        return {'strategies': len(sel), 'fills': n, 'excess_bp': round(weighted / n, 3)}

    return {
        'hours': hours,
        'rows': rows,
        'basis_rows': len(series),
        'gated': _arm(True),
        'control': _arm(False),
    }


def summary_line(hours=24):
    """One line for the monitor log. Never raises -- monitor swallows it anyway."""
    try:
        rep = population_edge(hours)
    except Exception as e:
        return f'Basis edge: unavailable ({e})'
    if not rep['basis_rows']:
        return 'Basis edge: no recorded basis history yet'
    g, c = rep['gated'], rep['control']
    if not rep['rows']:
        return (f"Basis edge: {rep['basis_rows']} recorded rows, "
                f"no fills matched in the last {hours}h")
    return (f"Basis edge ({hours}h, {rep['basis_rows']} rows): "
            f"gated {g['excess_bp']} bp over {g['fills']} fills / {g['strategies']} strats, "
            f"control {c['excess_bp']} bp over {c['fills']} fills / {c['strategies']} strats")


if __name__ == '__main__':
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24
    report = population_edge(hours)
    print(summary_line(hours))
    print()
    print(f"{'strategy':<28} {'arm':<8} {'n':>5} {'edge':>8} {'null':>8} "
          f"{'excess':>8} {'t':>7} {'cover':>6}  verdict")
    for r in report['rows']:
        verdict = {True: 'beats null', False: 'no edge', None: '--'}[r['beats_basis_null']]
        print(f"{r['name'][:28]:<28} {'gated' if r['gated'] else 'control':<8} "
              f"{r['basis_edge_n']:>5} {r['basis_edge_bp']:>8} "
              f"{r['basis_edge_null_bp']:>8} {r['basis_edge_excess_bp']:>8} "
              f"{str(r['basis_edge_t']):>7} {r['coverage']:>6}  {verdict}")

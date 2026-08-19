#!/usr/bin/env python3
"""Background job: find forecasts whose market has settled, record the real outcome.

The piece KALSHI.md calls out as having no existing precedent in this codebase: nothing
else here tracks "an external fact will become knowable at an unpredictable future time
and a log needs updating in place." kalshi_recorder.py's submit_forecast() logs a
`{'type': 'forecast', 'ticker', 'p_hat', 'p_market'}` row the instant a strategy
forecasts, with no outcome -- because there isn't one yet. This module is what closes
that loop: for every strategy's log, it finds tickers with unresolved forecasts, asks
kalshi_api.get_resolved() whether Kalshi has since settled them, and appends a
`{'type': 'resolution', 'ticker', 'outcome'}` row when it has.

APPEND, NEVER MUTATE
=====================
No forecast row is ever rewritten in place -- this codebase's convention for
daemon-written JSONL is append-only (see market_recorder.py's docstring), and mutating a
strategy's own log in place from a SEPARATE process racing against that strategy's own
appends is a straightforward way to corrupt it. One resolution row settles every
forecast row ever logged against that ticker in one pass; kalshi_recorder.cumulative_brier
does the join at read time.

WHY THIS MUST BE A SUPERVISED DAEMON, NOT A CALL INSIDE score_path()
=========================================================================
Checking every open ticker's settlement status is a network call per ticker.
score_path() runs on monitor's hourly scoring pass across the whole population and must
stay cheap (it reads the log domain_kalshi.py already has, via
kalshi_recorder.cumulative_brier -- no network at all). Reconciliation is the expensive,
async half, on its own clock, exactly like kalshi_recorder.py's daemon is for market
snapshots -- and for the identical reason: real Kalshi settlement is real external state
this process does not control the timing of.

TRUST MODEL -- read before assuming a resolved row is untamperable
========================================================================
See kalshi_recorder.py's module docstring and forecast_engine.py's TRUST MODEL note: the
revision LLM runs with full read/exec access as root and could, in principle, edit a
strategy's own trades/<name>.log directly to fabricate a resolution row. Nothing in this
module defends against that -- it is the same threat model sdex already accepts, and per
KALSHI.md's Phase 1 lessons, the real defense (check_boundary_integrity() watching
/opt/tools's git history) needs to explicitly cover trades/*.log before Phase 2, not just
this daemon's own liveness. Recording that seam here, not fixing it here.
"""
import glob
import json
import os
import sys
import time
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import kalshi_api
import kalshi_recorder

TRADES_DIR = Path('/opt/trades')
RECONCILE_INTERVAL = 300   # matches kalshi_recorder's cadence; no point polling faster


def _log_path(name):
    return TRADES_DIR / f'{name}.log'


def _pending_tickers(name):
    """Distinct tickers with at least one 'forecast' row and no 'resolution' row yet."""
    log_path = _log_path(name)
    if not log_path.exists():
        return set()
    forecast_tickers, resolved_tickers = set(), set()
    try:
        with log_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ticker = row.get('ticker')
                if not ticker:
                    continue
                if row.get('type') == 'forecast':
                    forecast_tickers.add(ticker)
                elif row.get('type') == 'resolution':
                    resolved_tickers.add(ticker)
    except Exception:
        return set()
    return forecast_tickers - resolved_tickers


def reconcile_strategy(name):
    """Check every one of `name`'s pending tickers against Kalshi; append a
    'resolution' row for each that has settled. Returns how many were newly resolved."""
    pending = _pending_tickers(name)
    if not pending:
        return 0
    resolved_now = 0
    log_path = _log_path(name)
    for ticker in pending:
        resolved = kalshi_api.get_resolved(ticker)
        if resolved is None:
            continue
        outcome = 1.0 if resolved['result'] == 'yes' else 0.0
        row = {'type': 'resolution', 'timestamp': time.time(),
               'ticker': ticker, 'outcome': outcome}
        try:
            with open(log_path, 'a') as f:
                f.write(json.dumps(row) + '\n')
        except Exception:
            continue
        resolved_now += 1
    return resolved_now


def _strategy_names():
    """Every name with a trades log, discovered by directory listing rather than
    /opt/strategy_state.json -- a retired or culled strategy's forecasts still deserve
    reconciliation, and this daemon should not need to know monitor's state format."""
    names = set()
    for path in glob.glob(str(TRADES_DIR / '*.log')):
        stem = Path(path).stem
        if stem.startswith('.'):
            continue   # e.g. a stray .something.log, not a strategy
        names.add(stem)
    return names


def reconcile_all():
    """One pass over every strategy's log. Returns {name: newly_resolved_count} for
    strategies that had at least one. Every strategy individually guarded -- one
    corrupt log must cost that strategy's reconciliation, not the whole pass."""
    out = {}
    for name in _strategy_names():
        try:
            n = reconcile_strategy(name)
        except Exception as e:
            print(f'[kalshi_reconcile] {name}: {e}', flush=True)
            continue
        if n:
            out[name] = n
    return out


def daemon(interval=RECONCILE_INTERVAL):
    """Reconcile forever. The single writer for resolution rows -- see module docstring's
    APPEND, NEVER MUTATE section for why more than one writer per log would be unsafe."""
    while True:
        try:
            results = reconcile_all()
            if results:
                print(f'{time.time()} resolved: {results}', flush=True)
        except Exception as e:
            print(f'reconcile pass failed ({e})', flush=True)
        try:
            now = time.time()
            time.sleep(max(1.0, interval - (now % interval)))
        except Exception:
            time.sleep(interval)


if __name__ == '__main__':
    if '--daemon' in sys.argv:
        every = RECONCILE_INTERVAL
        if '--interval' in sys.argv:
            try:
                every = int(sys.argv[sys.argv.index('--interval') + 1])
            except Exception:
                pass
        print(f'reconciling every {every}s', flush=True)
        daemon(every)
    else:
        name = sys.argv[1] if len(sys.argv) > 1 else None
        if name:
            print(f'pending for {name}: {sorted(_pending_tickers(name))}')
            print(f'newly resolved: {reconcile_strategy(name)}')
            print('cumulative_brier:', kalshi_recorder.cumulative_brier(name))
        else:
            print(json.dumps(reconcile_all(), indent=2))

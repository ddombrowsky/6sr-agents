#!/usr/bin/env python3
"""A durable record of what every Blend venue paid, and which strategies were alive.

ONE writer (the daemon below, supervised by domain_yield.ensure_background_jobs), MANY
readers. That single-writer property is the safety property, exactly as it is for
market_recorder.py: a full venue census is ~100 sequential RPC round trips, and a
population of twenty strategies each fetching its own would be a rate-limit incident
rather than a signal. Strategies read the snapshot this writes; the scorer reads the
history it appends.

WHY IT EXISTS. Two things in this domain cannot be measured without a written record,
and both decide rankings:

  * **What a venue paid, when.** Contract state reads are current-value only -- there is
    no "state as of last Tuesday" call -- and public Soroban RPC retains about seven days
    of events and no state history at all. A rate that is not written down at the time is
    gone. This file is therefore the beginning of YIELD.md step 2's artifact, recorded
    forward instead of backfilled, and it is what makes a rotation scoreable at all: the
    strategy chooses venues, this file supplies the rates, and neither can be edited by
    the other.
  * **Which strategies were actually running.** A supplied position keeps paying whether
    or not the process that chose it is alive, so a strategy stopped by the cull would go
    on accruing yield in any replay that did not know it was stopped -- and monitor
    restarts top-N stopped strategies, so it could be resurrected on returns it earned
    while dead. `live` below is the fix, and it is deliberately an OBSERVATION rather
    than something a strategy reports: the pid is checked from outside. A rule that
    decides ranking must never depend on the cooperation of the thing being ranked.

CADENCE. 300s, not market_recorder's 60s, because a census costs ~21s warm and hammering
a public RPC endpoint at a 35% duty cycle to resolve rate changes that arrive with
borrow/repay events would be rude and pointless. 300s gives 864 samples across the 3-day
scoring window, against a minimum hold measured in hours. If finer resolution is ever
needed, the way to get it is a batched `getLedgerEntries` over every reserve's ResData
key -- one round trip instead of forty -- not a shorter sleep.

Deliberately append-only JSONL. A partially-written last line costs one row rather than
the file, and everything here degrades to None rather than raising: this is called from a
daemon that must outlive every cycle it spans.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yield_venues

HISTORY_PATH = Path(os.environ.get('YIELD_HISTORY', '/opt/trades/.yield_history.jsonl'))
STATE_FILE = Path(os.environ.get('STRATEGY_STATE', '/opt/strategy_state.json'))

RECORD_INTERVAL = 300

# ~1 row per 5 minutes, so this is about 41 days -- the same horizon market_recorder
# keeps, reached with a fifth of the rows.
MAX_ROWS = 12000

# An OVER-estimate of a row, used only as the cheap size guard in _trim(). Under-estimate
# it and the guard trips while the file is still short of MAX_ROWS, at which point _trim
# rewrites the entire file on every append, forever, and never gets back under the guard.
# market_recorder.py learned this the expensive way; the number here is ~2x a measured
# 20-reserve row.
_ROW_BYTES = 8000


def live_strategies():
    """Names strat_manager has marked running AND whose pid is actually alive.

    The status field alone is not enough: it is written on start and stop and only
    reconciled once per monitor cycle, which is eight hours in the deployed container, so
    a crashed strategy reads as running for most of a scoring window. Checking the pid
    turns that into an observation. Signal 0 delivers nothing and only asks whether the
    process exists.
    """
    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        return []
    alive = []
    for name, entry in (state or {}).items():
        if not isinstance(entry, dict) or entry.get('status') != 'running':
            continue
        pid = entry.get('pid')
        if not pid:
            continue
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError, TypeError):
            continue
        alive.append(name)
    return sorted(alive)


def record_sample():
    """One census: refresh the snapshot strategies read, append a row here. Row or None.

    The snapshot and the history come from the SAME census on purpose. Two censuses would
    be twice the RPC and, worse, two slightly different views of the same moment -- the
    scorer would then be pricing a decision the strategy made against rates it never saw.
    """
    started = time.time()
    payload = yield_venues.write_snapshot()
    if payload is None:
        return None
    row = {
        'ts': int(payload.get('as_of', started)),
        'rows': yield_venues.allocatable_reserves(payload),
        'live': live_strategies(),
        'sample_s': round(time.time() - started, 1),
    }
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open('a') as handle:
            handle.write(json.dumps(row) + '\n')
        _trim()
    except Exception as e:
        print(f'could not append {HISTORY_PATH}: {e}', flush=True)
        return None
    return row


def _trim():
    """Keep the file under MAX_ROWS. Rewrites via a temp file so a reader mid-trim sees
    either the old file or the new one, never a truncated one."""
    try:
        if not HISTORY_PATH.exists():
            return
        if os.path.getsize(HISTORY_PATH) < MAX_ROWS * _ROW_BYTES:
            return
        lines = HISTORY_PATH.read_text().splitlines()
        if len(lines) <= MAX_ROWS:
            return
        tmp = HISTORY_PATH.with_suffix('.tmp')
        tmp.write_text('\n'.join(lines[-MAX_ROWS:]) + '\n')
        tmp.replace(HISTORY_PATH)
    except Exception:
        pass


def history(since=None, until=None):
    """Rows in [since, until], oldest first. Whole file if both are None.

    Read in full rather than tailed: the scorer wants a window, not a last row, and at
    MAX_ROWS this is a few tens of megabytes read once per scoring pass.
    """
    out = []
    try:
        if not HISTORY_PATH.exists():
            return out
        with HISTORY_PATH.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue          # a torn last line costs one row
                ts = row.get('ts')
                if not ts:
                    continue
                if since is not None and ts < since:
                    continue
                if until is not None and ts > until:
                    continue
                out.append(row)
    except Exception:
        pass
    return out


def tail(n=1, max_bytes=262144):
    """The last `n` rows without reading the whole file."""
    try:
        if not HISTORY_PATH.exists():
            return []
        size = os.path.getsize(HISTORY_PATH)
        with HISTORY_PATH.open('rb') as handle:
            handle.seek(max(0, size - max_bytes))
            chunk = handle.read().decode('utf-8', 'ignore')
    except Exception:
        return []
    rows = []
    for line in chunk.splitlines()[-(n + 1):]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows[-n:]


def span():
    """{'rows', 'first_ts', 'last_ts', 'hours'} -- how much history exists.

    What ensure_background_jobs prints each cycle, and what the scorer must consult
    before trusting a window: a score computed over a window the recorder only half
    covers is not a small error, it is a comparison against a null that ran longer.
    """
    out = {'rows': 0, 'first_ts': None, 'last_ts': None, 'hours': 0.0}
    try:
        if not HISTORY_PATH.exists():
            return out
        with HISTORY_PATH.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = json.loads(line).get('ts')
                except Exception:
                    continue
                if not ts:
                    continue
                out['rows'] += 1
                if out['first_ts'] is None:
                    out['first_ts'] = ts
                out['last_ts'] = ts
    except Exception:
        pass
    if out['first_ts'] and out['last_ts']:
        out['hours'] = round((out['last_ts'] - out['first_ts']) / 3600.0, 2)
    return out


def daemon(interval=RECORD_INTERVAL):
    """Sample forever. Sleeps the REMAINDER of the interval, not the whole of it.

    A census takes ~21s and can take much longer when RPC is slow; sleeping a flat
    interval on top of that lets the cadence drift, and a drifting sample clock makes
    every duration the scorer computes slightly wrong in a direction that depends on how
    the network felt.
    """
    while True:
        started = time.time()
        try:
            row = record_sample()
            if row is None:
                print('census failed; will retry next interval', flush=True)
            else:
                print(f"{row['ts']} {len(row['rows'])} venues, "
                      f"{len(row['live'])} live, {row['sample_s']}s", flush=True)
        except Exception as e:
            print(f'{type(e).__name__}: {e}', flush=True)
        time.sleep(max(1.0, interval - (time.time() - started)))


if __name__ == '__main__':
    if '--daemon' in sys.argv:
        every = RECORD_INTERVAL
        if '--interval' in sys.argv:
            try:
                every = int(sys.argv[sys.argv.index('--interval') + 1])
            except Exception:
                pass
        print(f'recording {HISTORY_PATH} every {every}s', flush=True)
        daemon(every)
    elif '--record' in sys.argv:
        print(json.dumps(record_sample(), indent=2, default=str))
    else:
        print('span:', json.dumps(span(), indent=2))
        print('live:', json.dumps(live_strategies()))
        last = tail(1)
        print('last row:', json.dumps(last[0], indent=2, default=str) if last
              else 'no history recorded yet')

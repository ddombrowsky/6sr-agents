#!/usr/bin/env python3
"""Restart monitor.py against freshly deployed code, WITHOUT an emperor revision pass.

Run this after `copy.sh --to` puts a new monitor.py, domain module or tool in place: the
running monitor imported the old code at startup and keeps using it for the life of the
process, so a deploy does not take effect until the process is replaced.

    docker exec $(cat .containername) /opt/restart_monitor.py
    docker exec $(cat .containername) /opt/restart_monitor.py --dry-run

WHY NOT JUST KILL IT, and why not the cooperative stop either. Three ways to stop a
monitor, and on a normal deploy all three of the obvious ones are wrong:

  1. `touch /opt/.monitor.py.exit` (the cooperative stop) is the gentlest, and it is what
     emperor.sh uses at window expiry. But sleep_or_exit() only reads that file BEFORE it
     sleeps, so a monitor already inside `time.sleep(CYCLE_SLEEP)` -- 8h in this
     container -- finishes its whole nap AND runs one more full cycle on the stale
     in-memory code before it notices. When the point of the restart is new scoring, that
     next cycle culls and clones on exactly the code being replaced.

  2. Killing monitor.py while emperor.sh is still supervising it hands control straight to
     emperor.sh's `wait`, whose very next step is the emperor-agent LLM self-revision pass
     over master-agent.py, monitor.py, sr_agent_tools.py and strat_manager.py. That pass
     is the whole reason this script exists: it is expensive, it burns model quota, and it
     rewrites the same files that were just deployed, on the evidence of a log that was
     truncated mid-cycle by the kill. Nobody wants a self-revision triggered by a deploy.

  3. `supervisorctl restart emperor` alone does NOT stop the monitor. emperor.sh launches
     it with setsid, into its own session, so supervisor's killasgroup never reaches it --
     this is the documented 2026-08-20 incident: the orphan kept /opt/.monitor.lock, every
     fresh monitor exited instantly on the held lock, and emperor.sh ran 48 complete cycles
     in 48 seconds, whose log pruning deleted every log from the run that had been working.

So the order below is the load-bearing part, and it is only three steps:

    stop emperor  ->  kill the (now orphaned) monitor  ->  start emperor

Stopping emperor FIRST is what suppresses the revision pass: with the supervisor gone
there is no `wait` left to return, so killing the monitor reaps nothing and triggers
nothing. Starting emperor last brings up a fresh monitor on the new code, and emperor.sh
clears the stale exit file itself at startup.

WHAT THIS DELIBERATELY DOES NOT TOUCH. Strategy processes. strat_manager starts each one
in its own process group, so none of them are in the monitor's group and none are affected
by any of this -- they keep quoting and keep writing fills to /opt/trades/<name>.log
throughout, which is what makes a restart cheap. A restart costs the remainder of one
cycle's sleep, nothing else.

Nor does it clear /opt/.monitor.lock. A stale lock is reclaimed by _acquire_lock() on the
next start (it checks whether the recorded pid is a LIVE monitor.py), and removing a lock
by hand is the one thing that lets two monitors run against the same state.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# Must match monitor.LOCK_FILE / monitor.EXIT_FILE and emperor.conf's [program:...] name.
LOCK_FILE = Path('/opt/.monitor.lock')
EXIT_FILE = Path('/opt/.monitor.py.exit')
SUPERVISOR_PROGRAM = os.environ.get('EMPEROR_PROGRAM', 'emperor')

# How long to wait for a TERMed monitor to go. It dies in about a second from a
# cycle-boundary sleep; the slow case is a cycle mid-flight, where the signal is handled
# once the current step returns. SIGKILL only after this.
TERM_GRACE_S = int(os.environ.get('RESTART_TERM_GRACE_S', 30))
# How long to wait for supervisor to report the fresh emperor RUNNING, and for that
# emperor to have spawned a monitor.
START_TIMEOUT_S = int(os.environ.get('RESTART_START_TIMEOUT_S', 60))


def run(cmd, check=True):
    """Run a command, returning its CompletedProcess. Output is captured, not printed."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _cmdline(pid):
    try:
        return Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\x00', b' ').decode(
            errors='replace')
    except OSError:
        return ''


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def find_monitors():
    """Every live monitor.py pid, by scanning /proc rather than trusting the lock file.

    The lock names at most one pid and can be stale in both directions; the failure this
    guards against is a second monitor nobody knows about surviving the restart and
    racing the fresh one on strategy_state.json.
    """
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        cmd = _cmdline(int(entry.name))
        if 'monitor.py' in cmd and 'restart_monitor' not in cmd:
            found.append((int(entry.name), cmd.strip()))
    return sorted(found)


def strategy_count():
    """How many strategy main.py processes are running -- printed before and after.

    These must be unaffected. Printing both numbers turns "the restart did not touch the
    strategies" from a claim in a docstring into something the operator watched happen.
    """
    return len([1 for entry in Path('/proc').iterdir()
                if entry.name.isdigit() and '/strategies/' in _cmdline(int(entry.name))
                and 'main.py' in _cmdline(int(entry.name))])


def stop_emperor(dry_run):
    print(f'1/3 stopping supervisor program {SUPERVISOR_PROGRAM!r} '
          f'(this is what suppresses the revision pass)')
    if dry_run:
        print('     [dry run] supervisorctl stop')
        return
    result = run(['supervisorctl', 'stop', SUPERVISOR_PROGRAM], check=False)
    output = (result.stdout + result.stderr).strip()
    if output:
        print(f'     {output}')
    if result.returncode != 0 and 'not running' not in output.lower():
        sys.exit(f'could not stop {SUPERVISOR_PROGRAM}; aborting rather than killing the '
                 f'monitor with its supervisor still live (that would run the emperor '
                 f'revision pass, which is the thing this script exists to avoid)')


def kill_monitors(dry_run):
    monitors = find_monitors()
    if not monitors:
        print('2/3 no monitor.py running; nothing to kill')
        return
    print(f'2/3 terminating {len(monitors)} orphaned monitor.py process(es)')
    for pid, cmd in monitors:
        print(f'     pid {pid}: {cmd}')
    if dry_run:
        print('     [dry run] SIGTERM, then SIGKILL after grace')
        return
    for pid, _cmd in monitors:
        try:
            os.kill(pid, 15)
        except OSError as e:
            print(f'     pid {pid} already gone ({e})')
    deadline = time.time() + TERM_GRACE_S
    while time.time() < deadline:
        if not any(_alive(pid) for pid, _ in monitors):
            print(f'     all exited cleanly')
            return
        time.sleep(0.5)
    for pid, _cmd in monitors:
        if _alive(pid):
            print(f'     pid {pid} ignored SIGTERM for {TERM_GRACE_S}s; sending SIGKILL')
            try:
                os.kill(pid, 9)
            except OSError:
                pass
    time.sleep(1)


def start_emperor(dry_run):
    print(f'3/3 starting {SUPERVISOR_PROGRAM!r}')
    if dry_run:
        print('     [dry run] supervisorctl start')
        return
    result = run(['supervisorctl', 'start', SUPERVISOR_PROGRAM], check=False)
    output = (result.stdout + result.stderr).strip()
    if output:
        print(f'     {output}')
    if result.returncode != 0:
        sys.exit(f'FAILED to start {SUPERVISOR_PROGRAM}. The system is now stopped -- '
                 f'no monitor, no emperor. Start it by hand: '
                 f'supervisorctl start {SUPERVISOR_PROGRAM}')
    deadline = time.time() + START_TIMEOUT_S
    while time.time() < deadline:
        monitors = find_monitors()
        if monitors:
            for pid, cmd in monitors:
                print(f'     monitor up: pid {pid}: {cmd}')
            return True
        time.sleep(1)
    print(f'     WARNING: no monitor.py appeared within {START_TIMEOUT_S}s. emperor.sh '
          f'may still be probing its interpreter, or monitor.py crashed on startup -- '
          f'check the newest /opt/emperor_logs/monitor_*.log', file=sys.stderr)
    return False


def main():
    parser = argparse.ArgumentParser(
        description='Restart monitor.py without triggering an emperor revision pass.')
    parser.add_argument('--dry-run', action='store_true',
                        help='print what would happen, change nothing')
    args = parser.parse_args()

    if not args.dry_run and os.geteuid() != 0:
        print('warning: not running as root; supervisorctl and kill may be refused',
              file=sys.stderr)

    before = strategy_count()
    print(f'{before} strategy process(es) running -- these are in their own process '
          f'groups and must be unaffected')
    if EXIT_FILE.exists():
        print(f'note: {EXIT_FILE} exists (a cooperative stop was already requested); '
              f'emperor.sh clears it at startup')
    print()

    stop_emperor(args.dry_run)
    kill_monitors(args.dry_run)
    started = start_emperor(args.dry_run)

    print()
    after = strategy_count()
    print(f'strategies: {before} before, {after} after'
          + ('' if after == before else '  <-- CHANGED, investigate'))
    if not args.dry_run:
        try:
            print(f'lock holder: {LOCK_FILE.read_text().strip()}')
        except OSError:
            print(f'lock holder: {LOCK_FILE} absent')
        print('\nthe fresh monitor is running the deployed code. Watch its first cycle:')
        print('  tail -f $(ls -t /opt/emperor_logs/monitor_*.log | head -1)')
    return 0 if (args.dry_run or started) else 1


if __name__ == '__main__':
    sys.exit(main())

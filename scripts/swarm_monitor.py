"""3-slot file-triggered emperor-agent launcher.

Watches SWARM_DIR for changes to slot-1.txt / slot-2.txt / slot-3.txt. When a
slot's file settles on new content (its mtime is unchanged across one poll
interval, so a still-being-written file is not read mid-write), that content
is piped as the opening turn into a fresh one-shot emperor-agent.py run
(main() already supports this via its `not sys.stdin.isatty()` branch). A
slot with a run still in progress is skipped; a change during that run is
picked up on the next poll after it exits, not queued or used to kill it.

Deployed by `copy.sh --to` to /opt/swarm_monitor.py, run via /opt/swarm.sh.
"""
import os
import signal
import subprocess
import sys
import time

SWARM_DIR = '/opt/agents/swarm'
AGENT_PATH = '/opt/agents/emperor-agent.py'
LOG_DIR = '/opt/emperor_logs'
SLOTS = ['slot-1', 'slot-2', 'slot-3']
POLL_INTERVAL = 5

_running = True


def _handle_sigterm(signum, frame):
    global _running
    _running = False


def _ensure_swarm_dir():
    os.makedirs(SWARM_DIR, exist_ok=True)
    for slot in SLOTS:
        path = os.path.join(SWARM_DIR, slot + '.txt')
        if not os.path.exists(path):
            open(path, 'w').close()


def _slot_path(slot):
    return os.path.join(SWARM_DIR, slot + '.txt')


def _launch(slot):
    stamp = time.strftime('%Y%m%d_%H%M%S')
    log_path = os.path.join(LOG_DIR, f'swarm-{slot}-{stamp}.log')
    log_file = open(log_path, 'w')
    print(f'[swarm] {slot}: launching, logging to {log_path}')
    proc = subprocess.Popen(
        [sys.executable, AGENT_PATH],
        stdin=open(_slot_path(slot)),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return proc, log_file


def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    os.makedirs(LOG_DIR, exist_ok=True)
    _ensure_swarm_dir()

    procs = {slot: None for slot in SLOTS}
    log_files = {slot: None for slot in SLOTS}
    last_seen_mtime = {slot: None for slot in SLOTS}
    last_dispatched_mtime = {slot: None for slot in SLOTS}

    print(f'[swarm] watching {SWARM_DIR}, interpreter {sys.executable}')

    while _running:
        for slot in SLOTS:
            proc = procs[slot]
            if proc is not None:
                if proc.poll() is not None:
                    print(f'[swarm] {slot}: exited with code {proc.returncode}')
                    log_files[slot].close()
                    procs[slot] = None
                    log_files[slot] = None
                continue

            mtime = os.path.getmtime(_slot_path(slot))
            if mtime == last_seen_mtime[slot] and mtime != last_dispatched_mtime[slot]:
                procs[slot], log_files[slot] = _launch(slot)
                last_dispatched_mtime[slot] = mtime
            last_seen_mtime[slot] = mtime

        time.sleep(POLL_INTERVAL)

    print('[swarm] shutting down, terminating in-flight runs...')
    for slot, proc in procs.items():
        if proc is not None:
            proc.terminate()


if __name__ == '__main__':
    main()

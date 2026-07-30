#!/usr/bin/env python3
import os
import json
import subprocess
import signal
import sys
from pathlib import Path

STATE_FILE = Path('/opt/strategy_state.json')
STRATEGIES_DIR = Path('/opt/strategies')
STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)

def load_state():
    if STATE_FILE.exists():
        with STATE_FILE.open('r') as f:
            return json.load(f)
    return {}

def save_state(state):
    with STATE_FILE.open('w') as f:
        json.dump(state, f, indent=2)

def clone_strategy(name, repo_url):
    target = STRATEGIES_DIR / name
    if target.exists():
        print(f"Strategy '{name}' already exists at {target}")
        return
    subprocess.check_call(['git', 'clone', repo_url, str(target)])
    print(f"Cloned strategy '{name}' into {target}")
    state = load_state()
    state[name] = {'path': str(target), 'pid': None, 'status': 'stopped'}
    save_state(state)

def start_strategy(name, command=None):
    state = load_state()
    if name not in state:
        print(f"Strategy '{name}' not known. Clone it first.")
        return
    if state[name].get('pid'):
        print(f"Strategy '{name}' already running (pid {state[name]['pid']}).")
        return
    strategy_path = Path(state[name]['path'])
    if not strategy_path.exists():
        print(f"Strategy path {strategy_path} does not exist.")
        return
    # Determine command: if provided use it, else try to run a python script named main.py inside the strategy dir
    if command is None:
        # try python main.py
        main_py = strategy_path / 'main.py'
        if main_py.exists():
            cmd = ['python3', str(main_py)]
        else:
            print(f"No command supplied and no main.py found for strategy '{name}'.")
            return
    else:
        # split command string into list for subprocess
        cmd = command if isinstance(command, list) else command.split()
    # Start process detached
    proc = subprocess.Popen(cmd, cwd=str(strategy_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
    state[name]['pid'] = proc.pid
    state[name]['status'] = 'running'
    save_state(state)
    print(f"Started strategy '{name}' with PID {proc.pid}")

def stop_strategy(name):
    state = load_state()
    if name not in state:
        print(f"Strategy '{name}' not known.")
        return
    pid = state[name].get('pid')
    if not pid:
        print(f"Strategy '{name}' is not running.")
        return
    try:
        # send SIGTERM to the process group
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        print(f"Sent termination to strategy '{name}' (PID {pid})")
    except ProcessLookupError:
        print(f"Process {pid} not found; cleaning up state.")
    state[name]['pid'] = None
    state[name]['status'] = 'stopped'
    save_state(state)

def list_strategies():
    state = load_state()
    if not state:
        print("No strategies registered.")
        return
    for name, info in state.items():
        print(f"{name}: path={info['path']}, status={info['status']}, pid={info.get('pid')}" )

def usage():
    print("Usage: strat_manager.py <command> [args]")
    print("Commands:")
    print("  clone <name> <repo_url>    Clone strategy repository")
    print("  start <name> [cmd]         Start strategy (optional custom command)")
    print("  stop <name>                Stop running strategy")
    print("  list                       List known strategies and status")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        usage()
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == 'clone' and len(sys.argv) == 4:
        _, _, name, repo = sys.argv
        clone_strategy(name, repo)
    elif cmd == 'start' and len(sys.argv) >= 3:
        name = sys.argv[2]
        custom_cmd = sys.argv[3:] if len(sys.argv) > 3 else None
        start_strategy(name, custom_cmd)
    elif cmd == 'stop' and len(sys.argv) == 3:
        stop_strategy(sys.argv[2])
    elif cmd == 'list':
        list_strategies()
    else:
        usage()
        sys.exit(1)

#!/usr/bin/env python3
import os
import json
import subprocess
import signal
import sys
import time
from pathlib import Path

STATE_FILE = Path('/opt/strategy_state.json')
STRATEGIES_DIR = Path('/opt/strategies')
STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
TRADES_DIR = Path('/opt/trades')  # also where run.log lives, alongside the trade logs
TRADES_DIR.mkdir(parents=True, exist_ok=True)
LIVE_STRATEGY_FILE = Path('/opt/live_strategy.json')  # written by monitor.py

# Preferred interpreter for a strategy's main.py, same path emperor.sh picks for monitor.
VENV_PYTHON = os.environ.get('STRATEGY_PYTHON', '/opt/agents/venv/bin/python')


def _strategy_python():
    """Which python runs a strategy's main.py.

    This used to be a bare 'python3', which resolves to /usr/bin/python3 under setsid,
    cron and `docker exec` -- the same word that had silently disabled the whole LLM
    layer (see emperor.sh's PYTHON comment). Here the cost is narrower but real: third
    party packages are installed into /opt/agents/venv, so a strategy started by
    /usr/bin/python3 raises ModuleNotFoundError on its first tick for anything the
    revision prompt's requirements manifest says is available.

    sys.executable is already correct whenever monitor.py spawned us, since it runs under
    emperor.sh's chosen interpreter and subprocesses inherit it. It is wrong in exactly
    one case: strat_manager started from its own `#!/usr/bin/env python3` shebang, which
    is how monitor invokes it (`/opt/strat_manager.py start ...`) and how an operator runs
    it by hand. `sys.prefix == sys.base_prefix` is the standard "not inside a venv" test,
    so this prefers the venv only when we are not already in one -- a monitor deliberately
    started under some other venv still wins.
    """
    if sys.prefix == sys.base_prefix and os.path.exists(VENV_PYTHON):
        return VENV_PYTHON
    return sys.executable


def load_live_strategy():
    if LIVE_STRATEGY_FILE.exists():
        try:
            return json.load(LIVE_STRATEGY_FILE.open())
        except Exception:
            return None
    return None

def live_strategy_name():
    live = load_live_strategy()
    return live.get('name') if live else None

def load_state():
    if STATE_FILE.exists():
        with STATE_FILE.open('r') as f:
            return json.load(f)
    return {}

def save_state(state):
    with STATE_FILE.open('w') as f:
        json.dump(state, f, indent=2)

def _commit_clone_name(target, name):
    """Commit the config.json name rewrite, so a fresh clone starts git-clean.

    monitor.revision_changed_anything() decides whether the revision model actually
    did anything by asking whether the clone is dirty or its HEAD moved since the
    clone. The rewrite below dirtied config.json before revise-strategy was even
    invoked, so that check answered True for every clone unconditionally: a model
    that replied in prose and never called a tool (clone_a006e0460235, 2026-08-04 --
    it printed a main.py diff in its final message and wrote nothing) was recorded as
    a successful "(llm)" revision, the apply_random_tweak fallback was skipped
    because `revised` stayed True, and a byte-identical copy of the parent was
    started. The gate's own docstring names that exact scenario as what it prevents.

    The rewrite belongs to the clone, not to the revision, so commit it here and let
    the dirty check mean what it says. Also puts the name in the committed history
    rather than only the working tree, which is what the next generation's `git
    clone` copies. Non-fatal on failure: the clone is still usable, it just costs
    that signal for this one strategy.
    """
    def git(*args):
        return subprocess.run(['git', '-C', str(target), *args],
                              capture_output=True, text=True)

    if git('diff', '--quiet', '--', 'config.json').returncode == 0:
        return  # untracked, or the name already matched
    commit_args = ['commit', '-m', f'clone: set config name to {name}',
                   '--', 'config.json']
    if not git('config', 'user.email').stdout.strip():
        # Fresh container: git has no identity configured and commit would abort.
        commit_args = ['-c', 'user.email=monitor@localhost',
                       '-c', 'user.name=strat_manager.py'] + commit_args
    r = git(*commit_args)
    if r.returncode != 0:
        print(f"Warning: could not commit config.json name for '{name}': "
              f"{r.stderr.strip() or r.stdout.strip()}")


def clone_strategy(name, repo_url):
    target = STRATEGIES_DIR / name
    if target.exists():
        print(f"Strategy '{name}' already exists at {target}")
        return
    subprocess.check_call(['git', 'clone', repo_url, str(target)])
    print(f"Cloned strategy '{name}' into {target}")
    # The cloned config.json still carries the source repo's "name" (e.g. the
    # template's "template_agent"), which main.py uses as the trade-log
    # filename. Rewrite it to the clone's own name immediately so trades never
    # collide with the parent's or a sibling clone's log file, regardless of
    # whether a later revision step (LLM or random tweak) also touches it.
    cfg_path = target / 'config.json'
    if cfg_path.exists():
        try:
            cfg = json.load(cfg_path.open())
            cfg['name'] = name
            json.dump(cfg, cfg_path.open('w'), indent=2)
        except Exception as e:
            print(f"Warning: could not update config.json name for '{name}': {e}")
        else:
            _commit_clone_name(target, name)
    state = load_state()
    state[name] = {'path': str(target), 'pid': None, 'status': 'stopped'}
    save_state(state)

def _is_alive(pid):
    # Deliberately NOT os.kill(pid, 0), which was what this used to be: that
    # succeeds on a zombie, and every strategy here becomes one and stays one.
    # start_strategy detaches main.py and then exits (monitor invokes this file as
    # a subprocess), so the strategy is orphaned onto PID 1 -- which in the
    # agenttest container is `sleep infinity`, an init that never wait()s. Nothing
    # reaps it, the PID entry survives, and os.kill(pid, 0) reports it alive
    # forever. So stop_strategy polled out the full STOP_GRACE_SECONDS and
    # SIGKILLed a corpse on every single cull ("ignored SIGTERM for 15s" on a
    # process that had died instantly), reconcile_state could never detect a
    # crashed strategy, and 537 zombies had accumulated in under three days.
    # Reading the state field from /proc is the only way to tell the difference.
    if not pid:
        return False
    try:
        with open(f'/proc/{pid}/stat') as f:
            # comm is field 2 and can contain spaces and parens, so split on the
            # LAST ')' -- state is the first field after it.
            state = f.read().rpartition(')')[2].split()[0]
        return state != 'Z'
    except FileNotFoundError:
        return False
    except (PermissionError, IndexError, OSError):
        # Can't tell -- assume alive, as the old PermissionError branch did.
        # Over-reporting alive costs a grace period; under-reporting would let
        # the cull believe it killed something that is still trading.
        return True

def reconcile_state():
    # A strategy that crashes (as opposed to being stopped via stop_strategy)
    # leaves state.json saying status='running' with a now-dead pid forever,
    # since nothing else ever rewrites that entry. monitor.py's self-heal
    # loop only restarts entries explicitly marked 'stopped', so a crashed
    # strategy would otherwise never be noticed or restarted. Call this once
    # per cycle to correct any such stale entries before ranking/restarting.
    state = load_state()
    changed = False
    for name, info in state.items():
        if info.get('status') == 'running' and not _is_alive(info.get('pid')):
            print(f"Strategy '{name}' has a dead pid ({info.get('pid')}); marking stopped.")
            info['pid'] = None
            info['status'] = 'stopped'
            changed = True
    if changed:
        save_state(state)
    return state

def prune_zombies():
    # A strategy whose clone/revise step got cut short (e.g. monitor.py's whole
    # process group killed mid-cycle by emperor.sh's window timeout while a
    # revise-strategy subprocess was still running) ends up fully checked out on
    # disk but never actually started: status='stopped', pid=None, and no
    # state.json ever written since main.py never ran once. Nothing else ever
    # notices or removes these -- they can't be scored (score_from_strategy_path
    # returns None forever), so they just print "Error reading state" and sort to
    # -inf every single cycle indefinitely. Drop any such entry from tracking;
    # the checked-out directory itself is left on disk for forensics.
    state = load_state()
    live = live_strategy_name()
    zombies = []
    for name, info in state.items():
        if info.get('status') != 'stopped' or info.get('pid'):
            continue
        if name == live:
            continue  # never untrack the strategy holding a real pubnet position
        state_path = Path(info['path']) / 'state.json'
        if not state_path.exists():
            zombies.append(name)
    for name in zombies:
        print(f"Strategy '{name}' was cloned but never started (no state.json); pruning from tracking.")
        del state[name]
    if zombies:
        save_state(state)
    return zombies

def rm_strategy(name):
    state = load_state()
    if name not in state:
        print(f"Strategy '{name}' not known.")
        return
    if state[name].get('status') == 'running':
        print(f"Strategy '{name}' is running; stop it first with 'stop {name}'.")
        return
    if name == live_strategy_name():
        # Untracking the live strategy would strand a real pubnet position: monitor.py
        # could no longer score it, and nothing would ever wind it down.
        print(f"Strategy '{name}' is the live pubnet strategy; refusing to remove it.")
        return
    path = state[name]['path']
    del state[name]
    save_state(state)
    print(f"Removed strategy '{name}' from tracking (files left on disk at {path}).")

def start_strategy(name, command=None):
    state = load_state()
    if name not in state:
        print(f"Strategy '{name}' not known. Clone it first.")
        return
    if state[name].get('pid') and _is_alive(state[name]['pid']):
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
            # -u: unbuffered stdout/stderr. Without it, Python fully buffers output when
            # it isn't a TTY, and SIGTERM (how stop_strategy ends a process) doesn't flush
            # that buffer — run.log would stay empty even though the process did real work.
            cmd = [_strategy_python(), '-u', str(main_py)]
        else:
            print(f"No command supplied and no main.py found for strategy '{name}'.")
            return
    else:
        # split command string into list for subprocess
        cmd = command if isinstance(command, list) else command.split()
    # Start process detached. stdout/stderr go to a per-strategy log file, not
    # /dev/null — now that a strategy can trigger real pubnet trades (submit_trade),
    # discarding its output would make live-trade failures permanently invisible.
    log_path = TRADES_DIR / f'{name}.run.log'
    log_file = open(log_path, 'a')
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(filter(None, ['/opt/tools', os.environ.get('PYTHONPATH')]))
    )
    proc = subprocess.Popen(cmd, cwd=str(strategy_path),
                            stdout=log_file, stderr=subprocess.STDOUT,
                            preexec_fn=os.setsid, env=env)
    state[name]['pid'] = proc.pid
    state[name]['status'] = 'running'
    save_state(state)
    print(f"Started strategy '{name}' with PID {proc.pid}, logging to {log_path}")

# How long a culled strategy gets to exit after SIGTERM before it is killed outright.
# main.py's loop sleeps 30s between ticks and does not install a signal handler, so the
# TERM lands almost immediately in practice; this only has to cover a tick that is mid
# network call to Horizon or a price feed.
STOP_GRACE_SECONDS = 15
STOP_POLL_SECONDS = 0.5


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
        # ...and then confirm it actually died. This used to mark the entry 'stopped'
        # the instant the signal was sent, without ever looking: a strategy that ignored
        # or outlived the TERM kept running, kept trading, and kept writing state.json,
        # while monitor.py believed it was culled -- so it was scored as a stopped
        # strategy, exempt from the restart loop, and only reconcile()'s dead-pid check a
        # full cycle later would notice anything was wrong (and it checks for the
        # opposite failure, so in this direction it noticed nothing at all). If it is
        # still alive after the grace period, escalate to SIGKILL, which nothing can
        # ignore. The cull has to be real: it is the only thing bounding how many
        # strategies run at once, and a live strategy that is believed dead is one
        # nothing will ever wind down.
        deadline = time.time() + STOP_GRACE_SECONDS
        while time.time() < deadline and _is_alive(pid):
            time.sleep(STOP_POLL_SECONDS)
        if _is_alive(pid):
            print(f"Strategy '{name}' (PID {pid}) ignored SIGTERM for "
                  f"{STOP_GRACE_SECONDS}s; sending SIGKILL")
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError) as e:
                print(f"Could not SIGKILL {pid}: {e}")
    except ProcessLookupError:
        print(f"Process {pid} not found; cleaning up state.")
    state[name]['pid'] = None
    state[name]['status'] = 'stopped'
    save_state(state)

def stop_all_strategies():
    state = load_state()
    running = [name for name, info in state.items() if info.get('status') == 'running']
    if not running:
        print("No running strategies.")
        return
    for name in running:
        stop_strategy(name)
    print(f"Stopped {len(running)} strategy(ies).")

def list_strategies():
    state = load_state()
    if not state:
        print("No strategies registered.")
        return
    live = load_live_strategy()
    live_name = live.get('name') if live else None
    for name, info in state.items():
        flag = ' **LIVE**' if name == live_name else ''
        print(f"{name}: path={info['path']}, status={info['status']}, pid={info.get('pid')}{flag}")

def usage():
    print("Usage: strat_manager.py <command> [args]")
    print("Commands:")
    print("  clone <name> <repo_url>    Clone strategy repository")
    print("  start <name> [cmd]         Start strategy (optional custom command)")
    print("  stop <name>                Stop running strategy")
    print("  stopall                    Stop all running strategies")
    print("  rm <name>                  Remove strategy from tracking (keeps files on disk; must be stopped first)")
    print("  list                       List known strategies and status")
    print("  reconcile                  Fix state entries stuck 'running' with a dead pid")
    print("  prune                      Remove clones that were never successfully started")

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
    elif cmd == 'stopall':
        stop_all_strategies()
    elif cmd == 'rm' and len(sys.argv) == 3:
        rm_strategy(sys.argv[2])
    elif cmd == 'list':
        list_strategies()
    elif cmd == 'reconcile':
        reconcile_state()
    elif cmd == 'prune':
        prune_zombies()
    else:
        usage()
        sys.exit(1)

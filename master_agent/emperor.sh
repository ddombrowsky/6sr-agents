#!/bin/sh

# Emperor script (higher than master)
#
# Runs forever by default, repeating this cycle:
# 1. Run monitor.py for a bounded window (EMPEROR_RUN_HOURS, default 12h, set
#    per-domain via env.sh -- 5m for DOMAIN=null), capturing its log,
#    then ask it to stop by touching /opt/.monitor.py.exit (it exits at its
#    next cycle-boundary sleep); TERM its process group only if that is still
#    not enough after another full window.
# 2. Feed that log plus the current SYSTEM_STATE.md to the
#    agents/emperor-agent.py agent, and have it revise:
#      * master_agent/master-agent.py
#      * master_agent/monitor.py
#      * master_agent/sr_agent_tools.py
#      * master_agent/strat_manager.py
#    committing any changes inside master_agent's own repo (and inside
#    tools' own repo, if it added/changed anything there).
# 3. Have it update /opt/SYSTEM_STATE.md with the current system summary.
# 4. Verify the four core files (and anything touched under /opt/tools)
#    still parse as valid Python; revert any file that doesn't, and
#    auto-commit anything the agent forgot to commit itself.
#
# Pass --once to run a single cycle and exit instead of looping forever.
#
# If /opt/emperor.sh.UNMANAGED exists at startup, this script does nothing but
# poll for its removal every 60s -- so a container can be brought up with
# emperor.sh as its entrypoint and left inert until deliberately enabled.
#
# Runs inside the agenttest container (paths below assume /opt is the mount
# point per ../create.sh). Override the run window for smoke-testing, e.g.:
#   EMPEROR_RUN_HOURS=2m ./emperor.sh --once

set -e

# Startup kill switch. While this file exists the script does nothing at all --
# no interpreter probe, no env.sh, no monitor.py -- it just polls for the file's
# removal, then carries on as normal. That makes it safe to wire emperor.sh in as
# a new container's startup command while still refining things by hand: create
# the file first, and enable the system later with a plain `rm` (no restart, no
# change to the container's command).
#
# Deliberately checked once, at startup, and not re-checked per cycle: stopping
# an emperor that is already running is what the cooperative-stop path
# ($MONITOR_EXIT_FILE, below) is for, and re-checking mid-loop would add a second
# way to halt with none of that path's guarantees about what is in flight.
#
# Kept out of /opt/master_agent on purpose: that is a git repo whose cleanliness
# monitor.py's check_boundary_integrity() watches, and verify_and_commit_repo()
# below would `git add -A` the sentinel straight into the history.
UNMANAGED_FILE="${EMPEROR_UNMANAGED_FILE:-/opt/emperor.sh.UNMANAGED}"
if [ -e "$UNMANAGED_FILE" ]; then
    echo "[emperor] $UNMANAGED_FILE exists -- UNMANAGED. Doing nothing;" \
         "polling every 60s. Remove that file to start."
    while [ -e "$UNMANAGED_FILE" ]; do
        sleep 60
    done
    echo "[emperor] $UNMANAGED_FILE removed -- starting."
fi

# Which python runs monitor.py and emperor-agent.py. This used to be a bare `python3`,
# and that single word disabled the entire LLM layer of this system for an unknown number
# of cycles: only /opt/agents/venv/bin/python has the `ollama` package, while a
# non-interactive shell (which is what setsid, cron and `docker exec` give you) resolves
# `python3` to /usr/bin/python3, which does not. Every revise-strategy subprocess died
# instantly with ModuleNotFoundError, monitor.py dutifully fell back to a random threshold
# tweak, and the logs looked like ordinary cycles. monitor.py now spawns revisions with
# sys.executable, so whatever is chosen here propagates all the way down.
#
# Prefer an interpreter that can actually import ollama; fall back to python3 so a
# container without the venv still runs (paper trading and culling work fine without a
# model, they just do not evolve).
PYTHON="${EMPEROR_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for candidate in /opt/agents/venv/bin/python python3; do
        if command -v "$candidate" >/dev/null 2>&1 && \
           "$candidate" -c 'import ollama' >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    done
fi
if [ -z "$PYTHON" ]; then
    PYTHON=python3
    echo "[emperor] WARNING: no interpreter found that can import ollama; falling back to" \
         "$PYTHON. Revisions and the emperor self-revision step will BOTH fail this run." >&2
fi
echo "[emperor] using interpreter: $PYTHON"

ONCE=0
for arg in "$@"; do
    case "$arg" in
        --once) ONCE=1 ;;
        *)
            echo "usage: $0 [--once]" >&2
            exit 1
            ;;
    esac
done

cd /opt
. ./env.sh

# How long monitor.py runs before this script asks it to stop and does its review pass.
# A WINDOW, not a period: the self-revision LLM call that follows it takes however long it
# takes, so the emperor cycle is this plus that.
#
# Read AFTER env.sh is sourced, deliberately -- that is how a domain sets its own cadence
# (a PACING dict in its domain module, which create.sh turns into exports in /opt/env.sh;
# see domain.py's group 8). DOMAIN=null runs a 5m window against a 120s CYCLE_SLEEP; sdex
# leaves both alone. Both are still overridable per-run from the environment.
RUN_HOURS=${EMPEROR_RUN_HOURS:-12h}
LOG_DIR=/opt/emperor_logs
# Cycles of logs kept, not hours of them: a domain that shortens the window above should
# raise this to match, or it keeps proportionally less history.
LOG_RETENTION_CYCLES=${EMPEROR_LOG_RETENTION:-48}
# Cooperative stop request read by monitor.py at every cycle-boundary sleep
# (see EXIT_FILE / sleep_or_exit there). Must match that path.
MONITOR_EXIT_FILE=/opt/.monitor.py.exit
mkdir -p "$LOG_DIR"

# Converts a timeout(1)/sleep(1)-style duration ("2.5h", "90m", "45s", or a
# bare number meaning seconds) into a plain integer number of seconds.
duration_to_seconds() {
    awk -v d="$1" 'BEGIN {
        unit = substr(d, length(d), 1)
        if (unit ~ /[0-9.]/) { printf "%d\n", d + 0; exit }
        num = substr(d, 1, length(d) - 1) + 0
        if (unit == "s") printf "%d\n", num
        else if (unit == "m") printf "%d\n", num * 60
        else if (unit == "h") printf "%d\n", num * 3600
        else if (unit == "d") printf "%d\n", num * 86400
        else printf "%d\n", num
    }'
}

# Deletes all but the most recent $LOG_RETENTION_CYCLES files of each log
# type, so emperor_logs/ doesn't grow unbounded across a script that's
# designed to run forever.
prune_logs() {
    for pattern in "monitor_*.log" "agent_*.log" "prompt_*.txt"; do
        # shellcheck disable=SC2086
        ls -1t "$LOG_DIR"/$pattern 2>/dev/null | tail -n +$((LOG_RETENTION_CYCLES + 1)) | while read -r f; do
            rm -f "$f"
        done
    done
}

# ast.parse-checks any changed .py file in $1 (a git repo); reverts (or
# deletes, if untracked) any file that fails to parse, then auto-commits
# whatever valid changes remain uncommitted, so a bad LLM self-edit can't
# silently brick the orchestrator and a forgotten commit doesn't silently
# lose work. $2 is just a label for log messages.
verify_and_commit_repo() {
    repo="$1"
    label="$2"
    [ -d "$repo/.git" ] || return 0

    changed_py_files=$(cd "$repo" && git status --porcelain | awk '{print $2}' | grep '\.py$' || true)

    for f in $changed_py_files; do
        path="$repo/$f"
        if [ -f "$path" ] && ! "$PYTHON" -c "import ast, sys
ast.parse(open(sys.argv[1]).read())" "$path" 2>>"$AGENT_LOG"; then
            echo "[emperor] WARNING: $label/$f failed to parse after this cycle's revision; reverting it" >&2
            if git -C "$repo" ls-files --error-unmatch "$f" >/dev/null 2>&1; then
                git -C "$repo" checkout -- "$f"
            else
                rm -f "$path"
            fi
        fi
    done

    if [ -n "$(git -C "$repo" status --porcelain)" ]; then
        echo "[emperor] $label has uncommitted changes after the revert pass; auto-committing as a fallback"
        git -C "$repo" add -A
        git -C "$repo" commit -m "emperor: auto-commit uncommitted changes from $STAMP" || true
    fi
}

# Floor on how fast this loop may spin, and an escalating backoff on top of it.
#
# Both are no-ops on a healthy cycle, which has already burned the whole $RUN_HOURS
# window by the time it reaches the delay. They only bind when monitor.py exits almost
# immediately -- which it does, in well under a second, whenever /opt/.monitor.lock is
# already held by a live monitor.py.
#
# That is not hypothetical. On 2026-08-20 monitor.py was still running from a previous
# emperor.sh when supervisor restarted this script: monitor.py is launched with setsid
# (see below), so it lives in its own session and supervisor's killasgroup never reached
# it. The orphan kept the lock, every fresh monitor.py refused to start and exited at
# once, and this loop -- which had no delay in it anywhere -- ran 48 complete cycles in
# 48 seconds. Each wrote a monitor log, a prompt and an agent log, and prune_logs keeps
# only $LOG_RETENTION_CYCLES of each, so the spin deleted every log from the run that
# had actually been working. A stuck dependency must not be able to erase the evidence
# of itself, which is what these two settings are for.
MIN_CYCLE_SECONDS=$(duration_to_seconds "${EMPEROR_MIN_CYCLE:-60}")
MAX_BACKOFF_SECONDS=$(duration_to_seconds "${EMPEROR_MAX_BACKOFF:-1h}")
# Doubles per *consecutive* suspiciously-short cycle, reset by the first normal one.
BACKOFF=0

while true; do
    CYCLE_START=$(date +%s)
    STAMP=$(date +%Y%m%d_%H%M%S)
    MONITOR_LOG="$LOG_DIR/monitor_$STAMP.log"
    AGENT_LOG="$LOG_DIR/agent_$STAMP.log"
    PROMPT_FILE="$LOG_DIR/prompt_$STAMP.txt"

    RUN_SECONDS=$(duration_to_seconds "$RUN_HOURS")
    echo "[emperor] running monitor.py for $RUN_HOURS (${RUN_SECONDS}s), logging to $MONITOR_LOG"

    # Clear any stale stop request (e.g. from a previous cycle whose monitor.py
    # was killed before it could consume the file) or this cycle's monitor.py
    # would exit on its very first sleep.
    rm -f "$MONITOR_EXIT_FILE"

    # monitor.py is launched in its own session (setsid) so that if the
    # cooperative stop below is ignored we can kill its whole process group,
    # not just monitor.py itself. monitor.py's revise-strategy calls are
    # synchronous, non-detached subprocess.run()s that can take up to
    # REVISION_TIMEOUT (60000s) each, times REVISIONS_PER_CYCLE (3) -- a single
    # monitor.py cycle can legitimately take longer than this window's default
    # so a plain `timeout monitor.py` would only kill monitor.py itself
    # and orphan any in-flight revise-strategy subprocess, leaving it running
    # unsupervised alongside the next cycle's fresh monitor.py.
    setsid "$PYTHON" -u /opt/monitor.py > "$MONITOR_LOG" 2>&1 &
    MONITOR_PID=$!
    MONITOR_START=$(date +%s)

    # Window expiry is a *request* first: touching $MONITOR_EXIT_FILE makes
    # monitor.py exit at its next cycle-boundary sleep, so it stops between
    # cycles with no revision subprocess, config rewrite or git commit in
    # flight. It only reads the file at those sleeps, so an already-sleeping
    # monitor takes up to one CYCLE_SLEEP to notice, and one still mid-cycle
    # takes however long that cycle has left -- hence a whole extra
    # $RUN_SECONDS of grace before falling back to the old behaviour of
    # killing the process group.
    (
        sleep "$RUN_SECONDS"
        echo "[emperor] window expired; requesting cooperative exit via $MONITOR_EXIT_FILE"
        : > "$MONITOR_EXIT_FILE"
        sleep "$RUN_SECONDS"
        echo "[emperor] WARNING: monitor.py still running ${RUN_SECONDS}s after $MONITOR_EXIT_FILE was placed; killing its process group" >&2
        kill -TERM -"$MONITOR_PID" 2>/dev/null
    ) &
    WATCHER_PID=$!

    wait "$MONITOR_PID" 2>/dev/null || true
    kill "$WATCHER_PID" 2>/dev/null || true
    wait "$WATCHER_PID" 2>/dev/null || true
    # monitor.py removes the file itself when it honours it; this covers the
    # TERM path and any exit that happened for an unrelated reason.
    rm -f "$MONITOR_EXIT_FILE"

    MONITOR_ELAPSED=$(( $(date +%s) - MONITOR_START ))
    # A cycle that ends in a small fraction of the requested window is
    # suspicious -- normally monitor.py runs right up to the window expiry,
    # so an early exit most likely means it crashed on startup (e.g. a
    # SyntaxError from a bad self-edit in a previous cycle) rather than
    # having genuinely finished early.
    if [ "$MONITOR_ELAPSED" -lt $((RUN_SECONDS / 10)) ]; then
        echo "[emperor] WARNING: monitor.py exited after only ${MONITOR_ELAPSED}s (window was ${RUN_SECONDS}s) -- possible crash-on-startup or a held /opt/.monitor.lock, check $MONITOR_LOG" >&2
        # Consecutive short cycles double the wait, so a dependency that stays stuck for
        # hours costs a handful of cycles rather than thousands.
        if [ "$BACKOFF" -eq 0 ]; then
            BACKOFF=$MIN_CYCLE_SECONDS
        else
            BACKOFF=$((BACKOFF * 2))
            if [ "$BACKOFF" -gt "$MAX_BACKOFF_SECONDS" ]; then
                BACKOFF=$MAX_BACKOFF_SECONDS
            fi
        fi
    else
        BACKOFF=0
    fi

    echo "[emperor] building analysis prompt at $PROMPT_FILE"
    {
        cat <<'HEADER'
You are the "emperor" review pass: higher-level than the master agent. You've
just been given the log from a multi-hour run of /opt/monitor.py (the hourly
strategy-culling/cloning loop) plus the current /opt/SYSTEM_STATE.md.

Your job, using your read_file/write_file/exec tools:

1. Read /opt/master_agent/master-agent.py, /opt/master_agent/monitor.py,
   /opt/master_agent/sr_agent_tools.py, and /opt/master_agent/strat_manager.py.
2. Based on what the monitor log below shows (errors, stalls, bad behavior,
   inefficiencies, or just opportunities for improvement), make concrete edits
   to any of those four files that would improve the system. Use write_file to
   apply them. If nothing meaningfully needs to change, it's fine to make no
   edits.
3. If any new utility scripts are needed, add them to the /opt/tools directory.
4. Commit whatever you changed:
     * If you changed anything under /opt/master_agent, commit it there (it
       is its own git repo), e.g. via exec:
         cd /opt/master_agent && git add -A && git commit -m "..."
     * If you added/changed anything under /opt/tools, commit that
       separately -- /opt/tools is its OWN independent git repo, not part of
       master_agent's, so a commit inside master_agent does not cover it:
         cd /opt/tools && git add -A && git commit -m "..."
5. Finally, update /opt/SYSTEM_STATE.md: read it first, then revise it in
   place to reflect the current system state (don't just replace it with an
   unrelated fresh draft -- preserve continuity across emperor runs). Write
   concise bullet points rather than long run-on paragraphs. Use write_file to
   save it back to /opt/SYSTEM_STATE.md.

=== Current /opt/SYSTEM_STATE.md ===
HEADER

        if [ -f /opt/SYSTEM_STATE.md ]; then
            cat /opt/SYSTEM_STATE.md
        else
            echo "(no SYSTEM_STATE.md exists yet)"
        fi

        echo
        echo "=== monitor.py log ($MONITOR_LOG, last 4000 lines) ==="
        tail -n 4000 "$MONITOR_LOG"
    } > "$PROMPT_FILE"

    echo "[emperor] running emperor-agent.py, logging to $AGENT_LOG"
    {
        cat "$PROMPT_FILE"
        printf '\nexit\n'
    } | "$PYTHON" /opt/agents/emperor-agent.py > "$AGENT_LOG" 2>&1 || \
        echo "[emperor] emperor-agent.py step failed this cycle; see $AGENT_LOG" >&2

    echo "[emperor] verifying and committing master_agent/tools changes"
    verify_and_commit_repo /opt/master_agent "master_agent"
    verify_and_commit_repo /opt/tools "tools"

    prune_logs

    echo "[emperor] cycle done. logs in $LOG_DIR"

    if [ "$ONCE" = "1" ]; then
        break
    fi

    # Hold the next cycle off until the floor (or the current backoff, whichever is
    # larger) has elapsed since this cycle STARTED, so the work already done counts
    # towards it and a normal cycle waits not at all.
    CYCLE_ELAPSED=$(( $(date +%s) - CYCLE_START ))
    DELAY=$MIN_CYCLE_SECONDS
    if [ "$BACKOFF" -gt "$DELAY" ]; then
        DELAY=$BACKOFF
    fi
    REMAINING=$(( DELAY - CYCLE_ELAPSED ))
    if [ "$REMAINING" -gt 0 ]; then
        echo "[emperor] cycle took ${CYCLE_ELAPSED}s; holding the next one off for ${REMAINING}s (floor ${MIN_CYCLE_SECONDS}s, backoff ${BACKOFF}s)"
        sleep "$REMAINING"
    fi
done

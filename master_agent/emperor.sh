#!/bin/sh

# Emperor script (higher than master)
#
# Runs forever by default, repeating this cycle:
# 1. Run monitor.py for a bounded window (default 2.5h), capturing its log.
# 2. Feed that log plus the current SYSTEM_STATE.md to the
#    agents/agent-bootstrap.py agent, and have it revise:
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
# Runs inside the agenttest container (paths below assume /opt is the mount
# point per ../create.sh). Override the run window for smoke-testing, e.g.:
#   EMPEROR_RUN_HOURS=2m ./emperor.sh --once

set -e

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

RUN_HOURS=${EMPEROR_RUN_HOURS:-2.5h}
LOG_DIR=/opt/emperor_logs
LOG_RETENTION_CYCLES=${EMPEROR_LOG_RETENTION:-48}
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
        if [ -f "$path" ] && ! python3 -c "import ast, sys
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

while true; do
    STAMP=$(date +%Y%m%d_%H%M%S)
    MONITOR_LOG="$LOG_DIR/monitor_$STAMP.log"
    AGENT_LOG="$LOG_DIR/agent_$STAMP.log"
    PROMPT_FILE="$LOG_DIR/prompt_$STAMP.txt"

    RUN_SECONDS=$(duration_to_seconds "$RUN_HOURS")
    echo "[emperor] running monitor.py for $RUN_HOURS (${RUN_SECONDS}s), logging to $MONITOR_LOG"

    # monitor.py is launched in its own session (setsid) so that on window
    # expiry we can kill its whole process group, not just monitor.py
    # itself. monitor.py's revise-strategy calls are synchronous,
    # non-detached subprocess.run()s that can take up to REVISION_TIMEOUT
    # (6000s) each, times two clones per cycle -- a single monitor.py cycle
    # can legitimately take longer than this window's default (2.5h), so a
    # plain `timeout monitor.py` would only kill monitor.py itself and
    # orphan any in-flight revise-strategy subprocess, leaving it running
    # unsupervised alongside the next cycle's fresh monitor.py.
    setsid python3 -u /opt/monitor.py > "$MONITOR_LOG" 2>&1 &
    MONITOR_PID=$!
    MONITOR_START=$(date +%s)

    (
        sleep "$RUN_SECONDS"
        kill -TERM -"$MONITOR_PID" 2>/dev/null
    ) &
    WATCHER_PID=$!

    wait "$MONITOR_PID" 2>/dev/null || true
    kill "$WATCHER_PID" 2>/dev/null || true
    wait "$WATCHER_PID" 2>/dev/null || true

    MONITOR_ELAPSED=$(( $(date +%s) - MONITOR_START ))
    # A cycle that ends in a small fraction of the requested window is
    # suspicious -- normally monitor.py runs right up to the window expiry,
    # so an early exit most likely means it crashed on startup (e.g. a
    # SyntaxError from a bad self-edit in a previous cycle) rather than
    # having genuinely finished early.
    if [ "$MONITOR_ELAPSED" -lt $((RUN_SECONDS / 10)) ]; then
        echo "[emperor] WARNING: monitor.py exited after only ${MONITOR_ELAPSED}s (window was ${RUN_SECONDS}s) -- possible crash-on-startup, check $MONITOR_LOG" >&2
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

    echo "[emperor] running agent-bootstrap.py, logging to $AGENT_LOG"
    {
        cat "$PROMPT_FILE"
        printf '\nexit\n'
    } | python3 /opt/agents/agent-bootstrap.py > "$AGENT_LOG" 2>&1 || \
        echo "[emperor] agent-bootstrap.py step failed this cycle; see $AGENT_LOG" >&2

    echo "[emperor] verifying and committing master_agent/tools changes"
    verify_and_commit_repo /opt/master_agent "master_agent"
    verify_and_commit_repo /opt/tools "tools"

    prune_logs

    echo "[emperor] cycle done. logs in $LOG_DIR"

    if [ "$ONCE" = "1" ]; then
        break
    fi
done

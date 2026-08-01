#!/bin/sh

# Emperor script (higher than master)
#
# Runs forever by default, repeating this cycle:
# 1. Run monitor.py for a bounded window (default 6h), capturing its log.
# 2. Feed that log plus the current SYSTEM_STATE.md to the
#    agents/agent-bootstrap.py agent, and have it revise:
#      * master_agent/master-agent.py
#      * master_agent/monitor.py
#      * master_agent/sr_agent_tools.py
#      * master_agent/strat_manager.py
#    committing any changes inside master_agent's own repo.
# 3. Have it update /opt/SYSTEM_STATE.md with the current system summary.
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
mkdir -p "$LOG_DIR"

while true; do
    STAMP=$(date +%Y%m%d_%H%M%S)
    MONITOR_LOG="$LOG_DIR/monitor_$STAMP.log"
    AGENT_LOG="$LOG_DIR/agent_$STAMP.log"
    PROMPT_FILE="$LOG_DIR/prompt_$STAMP.txt"

    echo "[emperor] running monitor.py for $RUN_HOURS, logging to $MONITOR_LOG"
    timeout "$RUN_HOURS" python3 /opt/monitor.py > "$MONITOR_LOG" 2>&1 || true

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
3. If you changed anything, commit it inside /opt/master_agent (it is its own
   git repo) e.g. via exec:
     cd /opt/master_agent && git add -A && git commit -m "..."
4. Finally, update /opt/SYSTEM_STATE.md: read it first, then revise it in
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

    echo "[emperor] cycle done. logs in $LOG_DIR"

    if [ "$ONCE" = "1" ]; then
        break
    fi
done

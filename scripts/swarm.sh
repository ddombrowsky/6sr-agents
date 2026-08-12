#!/bin/bash
#
# Run the 3-slot agent swarm monitor INSIDE the container:
#
#   docker exec <container> /opt/swarm.sh &
#
# Deployed by `copy.sh --to` to /opt/swarm.sh, next to once.sh and selftest.sh.
# Watches /opt/agents/swarm/slot-{1,2,3}.txt; a change to one launches a
# one-shot emperor-agent.py run with that file piped in as its opening turn.
# See scripts/swarm_monitor.py for the polling/dispatch logic.

# Same interpreter-resolution rationale as master_agent/emperor.sh: a bare
# `python3` in the non-interactive shell docker exec gives you resolves to
# /usr/bin/python3, which does not have the `ollama` package, so every
# emperor-agent.py run would silently fail. Prefer the venv interpreter that
# can actually import ollama; fall back to python3 so a container without
# the venv still runs.
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
    echo "[swarm] WARNING: no interpreter found that can import ollama; falling back to" \
         "$PYTHON. Swarm runs will fail to reach the model this run." >&2
fi
echo "[swarm] using interpreter: $PYTHON"

cd /opt
. ./env.sh

exec "$PYTHON" swarm_monitor.py

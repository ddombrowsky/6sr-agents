#!/bin/sh
#
# Run master_agent/selftest_domain.py inside the container, against the container's real
# /opt/tools and its real strategy population, WITHOUT deploying anything.
#
#   ./run-selftest-in-container.sh
#
# Log goes to selftest-logs/container-<timestamp>.log on the host, and a copy stays in the
# container at /root/domain-selftest-logs/. Exit status is the self-test's.
#
# WHY NOT JUST `copy.sh --to` AND RUN IT FROM /opt
# ================================================
# Two reasons, both of which would make this test cost something:
#
#  1. Copying into v/master_agent IS a deploy. It swaps the loop's code out from under a
#     running monitor, and it makes /opt/master_agent dirty -- which is exactly what
#     check_boundary_integrity() halts live trading on. This script is for checking the
#     refactor before you decide to deploy it, so it must not deploy it.
#  2. it tests the code in THIS working tree, including uncommitted changes, rather than
#     whatever was last deployed. That is what you want before deciding to deploy.
#
# So instead: copy this repo (with its .git) to a container-local path outside the /opt
# bind mount, and run the self-test from there. It imports the new modules from that copy,
# and reads /opt/strategy_state.json and /opt/strategies for the real-population checks.
#
# ONCE YOU HAVE DEPLOYED (`./copy.sh --to`), PREFER THE OTHER SCRIPT
# ==================================================================
# `docker exec <container> /opt/selftest.sh` tests the *deployed* code, finds its baseline
# in /opt/master_agent's own history, and additionally checks the container's health (the
# ollama import, the missing .gitignore that halts live trading, whether a watched repo is
# already dirty). This script is the pre-deploy check; that one is the post-deploy check.
#
# WHY /opt/tools IS COPIED TOO
# ============================
# Several tools cache to disk *next to themselves* -- ohlc_history writes
# .ohlc_cache_<interval>.json, friction writes .friction_cache.json. Nothing in
# /opt/tools ignores them, so a run that touched the real directory would leave untracked
# files behind, `git status --porcelain` would report them, and the next monitor cycle
# would print LIVE TRADING HALTED. So /opt/tools is copied to a scratch path that goes
# FIRST on PYTHONPATH. Every tool import resolves to the copy (domain_sdex and score.py
# both *append* '/opt/tools', so an earlier entry wins), the code under test is byte
# identical, and the caches land in the copy. Absolute paths the tools read -- /opt/trades,
# /opt/strategy_state.json -- are still the real ones, which is the point.
#
# The self-test writes nothing outside temp directories. The one exception is
# cleanup_scratch, which unlinks /opt/trades/<its own smoketest_*>.log.
set -e

CONTAINER="${CONTAINER:-$(cat "$(dirname "$0")/.containername")}"
REPO="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
HOST_LOG="$REPO/selftest-logs/container-$STAMP.log"

DEST=/root/domain-selftest
TOOLS=/root/tools-selftest
LOGDIR=/root/domain-selftest-logs

mkdir -p "$REPO/selftest-logs"

echo "container : $CONTAINER"
echo "log       : $HOST_LOG"
echo

# Refuse to run against a container that isn't up, rather than emitting 20 confusing
# docker errors.
docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true || {
    echo "$CONTAINER is not running. Start it (./create.sh) and try again." >&2
    exit 1
}

# Record what the integrity-watched repos looked like BEFORE, so the check at the end is
# meaningful rather than decorative.
before_tools=$(docker exec "$CONTAINER" git -C /opt/tools status --porcelain 2>&1 || true)
before_ma=$(docker exec "$CONTAINER" git -C /opt/master_agent status --porcelain 2>&1 || true)

# The repo, with .git (the differential needs it) and without v/ (it is the bind mount, it
# is large, and the container already has it as /opt).
echo "copying the repo to $CONTAINER:$DEST ..."
docker exec "$CONTAINER" rm -rf "$DEST"
docker exec "$CONTAINER" mkdir -p "$DEST" "$LOGDIR"
tar -C "$REPO" -cf - --exclude=./v --exclude=./selftest-logs --exclude=__pycache__ . \
    | docker exec -i "$CONTAINER" tar -C "$DEST" -xf -

echo "copying /opt/tools to $TOOLS (so cache writes cannot dirty the real one) ..."
docker exec "$CONTAINER" sh -c "rm -rf $TOOLS && cp -a /opt/tools $TOOLS && rm -rf $TOOLS/.git"

echo "running the self-test ..."
set +e
docker exec \
    -e PYTHONPATH="$TOOLS" \
    -e SMOKE_TEST_SECONDS=3 \
    -e GIT_CONFIG_GLOBAL=/dev/null \
    "$CONTAINER" sh -c "cd $DEST/master_agent && \
        git config --global --add safe.directory $DEST >/dev/null 2>&1; \
        /opt/agents/venv/bin/python selftest_domain.py 2>&1 | tee $LOGDIR/container-$STAMP.log; \
        exit \${PIPESTATUS:-\$?}" > "$HOST_LOG" 2>&1
rc=$?
set -e

tail -20 "$HOST_LOG"
echo
echo "full log: $HOST_LOG"
echo "          $CONTAINER:$LOGDIR/container-$STAMP.log"

# Did the run leave a mark on either repo the integrity check watches? A new untracked
# file here is not cosmetic: it halts live trading on the next cycle.
after_tools=$(docker exec "$CONTAINER" git -C /opt/tools status --porcelain 2>&1 || true)
after_ma=$(docker exec "$CONTAINER" git -C /opt/master_agent status --porcelain 2>&1 || true)
echo
if [ "$before_tools" = "$after_tools" ] && [ "$before_ma" = "$after_ma" ]; then
    echo "integrity: /opt/tools and /opt/master_agent unchanged by this run"
else
    echo "integrity: WARNING -- this run changed a watched repo:"
    echo "  /opt/tools before: [$before_tools]  after: [$after_tools]"
    echo "  /opt/master_agent before: [$before_ma]  after: [$after_ma]"
fi

exit $rc

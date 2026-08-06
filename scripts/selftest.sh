#!/bin/sh
#
# Run the domain-boundary self-test INSIDE the container, against the deployed code.
#
#   docker exec <container> /opt/selftest.sh
#
# Deployed by `copy.sh --to` to /opt/selftest.sh, next to once.sh and st.sh. Exits 0 if
# every check passed, 1 otherwise. Log goes to /opt/emperor_logs/selftest-<stamp>.log.
#
# This checks two different things, and both matter:
#
#   1. THE REFACTOR. master_agent/selftest_domain.py loads the pre-refactor monitor.py out
#      of /opt/master_agent's own git history -- it searches the history for the last
#      monitor.py that still defined _config_is_sane, fetch_marks_for_cycle and
#      apply_seed_thresholds -- and requires the old code and the new domain members to give
#      the same answers. Run here rather than on the host, it exercises the real order
#      books, the real recorded basis distribution, regime.suggest_bands' band grid and the
#      real strategy population; on the host a dozen code paths quietly take their degraded
#      branch instead.
#
#   2. THE CONTAINER. Three environmental faults that are invisible until they cost a whole
#      emperor window:
#        * the interpreter cannot import ollama. This killed the entire revision layer for
#          weeks while the logs read as ordinary cycles -- see
#          monitor._check_revision_interpreter.
#        * /opt/tools or /opt/master_agent is missing its .gitignore, so the first cache
#          write or the first __pycache__ leaves an untracked file, check_boundary_integrity
#          reports a dirty repo, and live trading halts. Both directions of copy.sh use
#          `cp dir/*`, a glob that does not match dotfiles, so a freshly-provisioned
#          container reliably arrives without them.
#        * a watched repo is already dirty, or HEAD has moved off the recorded baseline.
#
# It reports those and keeps going -- they are facts about the container, not failures of
# the refactor, and the exit status is the self-test's alone. The count is in the summary.
#
# WHY IT COPIES /opt/tools AND DISABLES .pyc
# ==========================================
# So that running the test cannot cause fault (2) itself. ohlc_history, friction, dex_price
# and price_feed all cache to disk *next to themselves*; importing them from /opt/tools
# would write .ohlc_cache_60.json and friends into a repo whose cleanliness gates real
# money. So /opt/tools is copied to a scratch path that goes FIRST on PYTHONPATH -- both
# domain_sdex and score.py *append* '/opt/tools', so an earlier entry wins, the code under
# test is byte-identical, and the caches land in the copy. PYTHONDONTWRITEBYTECODE stops
# the same thing happening to /opt/master_agent via __pycache__.
#
# The watched repos are diffed before against after, so pre-existing dirt is not blamed on
# this run and a mark this run *does* leave cannot hide behind it.
#
# Absolute paths stay real on purpose: /opt/trades, /opt/strategy_state.json and
# /opt/strategies are the live ones, because reading them is the point.
#
# This script deliberately does NOT write to /opt/tools or /opt/master_agent, not even to
# fix a missing .gitignore. A self-test that edits the real-money boundary is not a
# self-test. It prints the exact command instead.
set -e

OPT=/opt
MA=$OPT/master_agent
TOOLS_COPY=/root/tools-selftest
LOGDIR=$OPT/emperor_logs
STAMP=$(date +%Y%m%d-%H%M%S)
LOG=$LOGDIR/selftest-$STAMP.log
RCFILE=/tmp/selftest-rc.$$
PY=$OPT/agents/venv/bin/python

WATCHED="$OPT/tools $MA"

if [ ! -d "$MA" ]; then
    echo "$MA does not exist -- this script runs INSIDE the container." >&2
    echo "From the host, use ./run-selftest-in-container.sh instead." >&2
    exit 1
fi
if [ ! -f "$MA/selftest_domain.py" ]; then
    echo "$MA/selftest_domain.py is missing -- run ./copy.sh --to on the host first." >&2
    exit 1
fi
[ -x "$PY" ] || PY=python3

mkdir -p "$LOGDIR"
: > "$LOG"

# Everything goes through say(), in the MAIN shell rather than a `{ ... } | tee` block, so
# that `warnings` below is still incremented where the summary can read it. A pipeline runs
# its left side in a subshell and every count made in there is discarded on exit -- which is
# how the first version of this script reported "PASSED ( container warning(s))".
say() {
    printf '%s\n' "$*" | tee -a "$LOG"
}
sayfile() {
    sed 's/^/           /' "$1" | tee -a "$LOG"
}

say "=== domain self-test $STAMP ==="
say "interpreter : $PY ($($PY -V 2>&1))"
say "log         : $LOG"
say ""

# ---------------------------------------------------------------- 1. the container itself
warnings=0

# The failure that hid for weeks. Only a warning: selftest_domain.py stubs ollama, so the
# test itself is unaffected -- but a monitor started under this interpreter would fall back
# to a mechanical tweak on every clone and the log would look completely normal.
if $PY -c 'import ollama' >/dev/null 2>&1; then
    say "ok       $PY can import ollama"
else
    say "WARNING  $PY CANNOT import ollama -- a monitor started under it would fail every"
    say "         revision and fall back to a random tweak, silently. See"
    say "         monitor._check_revision_interpreter."
    warnings=$((warnings + 1))
fi

# The key monitor and master-agent need. Checked for presence only; never printed, and
# never sourced -- /opt/env.sh activates a venv by relative path, which fails from any
# other directory and would override the interpreter chosen above.
if [ -r "$OPT/env.sh" ] && grep -q OLLAMA_API_KEY "$OPT/env.sh" 2>/dev/null; then
    say "ok       $OPT/env.sh sets OLLAMA_API_KEY (value not read or logged)"
else
    say "WARNING  no OLLAMA_API_KEY in $OPT/env.sh -- every revision will fail with"
    say "         '[error: OLLAMA_API_KEY is not set]'."
    warnings=$((warnings + 1))
fi

# The .gitignore landmine, per watched repo. Untracked files here are not cosmetic: they are
# what check_boundary_integrity halts live trading on. Printed as a literal command via a
# quoted heredoc so the suggestion survives being echoed.
for repo in $WATCHED; do
    if [ -f "$repo/.gitignore" ]; then
        say "ok       $repo has a .gitignore"
        continue
    fi
    say "WARNING  $repo has no .gitignore. The first cache write or __pycache__ there will"
    say "         leave an untracked file, and the next cycle will print LIVE TRADING"
    say "         HALTED. copy.sh uses 'cp dir/*', which skips dotfiles, so it cannot"
    say "         deliver one. Fix by hand in the container, then commit it and delete"
    say "         $OPT/.integrity_baseline.json:"
    case "$repo" in
      */tools)
        cat <<'EOS' | tee -a "$LOG"
           cat > /opt/tools/.gitignore <<'IGN'
           __pycache__
           .ohlc_cache_*.json
           .asset_price_cache.json
           .asset_universe_cache.json
           .asset_verify_cache.json
           .friction_cache.json
           .xlm_price_cache.json
           .price_history_cache.json
           *.tmp
           *.tmp.*
           IGN
EOS
        ;;
      *)
        say "           echo __pycache__ > $repo/.gitignore"
        ;;
    esac
    warnings=$((warnings + 1))
done

# State of the money boundary, captured so the check at the end is a diff and not a
# restatement.
before_tools=$(git -C "$OPT/tools" status --porcelain 2>/dev/null || true)
before_ma=$(git -C "$MA" status --porcelain 2>/dev/null || true)
for repo in $WATCHED; do
    head=$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo '?')
    dirty=$(git -C "$repo" status --porcelain 2>/dev/null || true)
    if [ -n "$dirty" ]; then
        say "WARNING  $repo is ALREADY dirty at $head, before this run -- live trading is"
        say "         halted until this is committed and the baseline deleted:"
        printf '%s\n' "$dirty" | sed 's/^/           /' | head -10 | tee -a "$LOG"
        warnings=$((warnings + 1))
    else
        say "ok       $repo is clean at $head"
    fi
done

if [ -f "$OPT/.integrity_baseline.json" ]; then
    say "ok       $OPT/.integrity_baseline.json exists:"
    sayfile "$OPT/.integrity_baseline.json"
else
    say "note     no $OPT/.integrity_baseline.json yet; the next cycle adopts one if both"
    say "         repos are clean. That is the normal state right after a deploy."
fi

# Which strategy, if any, is trading real money. Worth stating plainly in a log that will
# be read after the fact.
if [ -f "$OPT/live_strategy.json" ]; then
    say "note     a live strategy is configured:"
    sayfile "$OPT/live_strategy.json"
else
    say "ok       no live strategy; nothing is trading real money"
fi

if [ -f "$OPT/emperor.sh.UNMANAGED" ]; then
    say "note     $OPT/emperor.sh.UNMANAGED exists, so emperor.sh is parked and no cycle is"
    say "         running. The population will be empty and the real-population"
    say "         differential below will skip."
fi
say ""

# ------------------------------------------------------- 2. isolate the caches, then run
say "copying $OPT/tools -> $TOOLS_COPY so cache writes cannot dirty the real one"
rm -rf "$TOOLS_COPY"
cp -a "$OPT/tools" "$TOOLS_COPY"
rm -rf "$TOOLS_COPY/.git"
say ""
say "--- selftest_domain.py ---"

set +e
(
  cd "$MA" || exit 1
  PYTHONPATH="$TOOLS_COPY" \
  PYTHONDONTWRITEBYTECODE=1 \
  SMOKE_TEST_SECONDS="${SMOKE_TEST_SECONDS:-3}" \
  "$PY" selftest_domain.py
  echo $? > "$RCFILE"
) 2>&1 | tee -a "$LOG"
rc=$(cat "$RCFILE" 2>/dev/null || echo 1)
rm -f "$RCFILE"
set -e

# ------------------------------------------------------------- 3. did the run leave marks
say ""
after_tools=$(git -C "$OPT/tools" status --porcelain 2>/dev/null || true)
after_ma=$(git -C "$MA" status --porcelain 2>/dev/null || true)
if [ "$before_tools" = "$after_tools" ] && [ "$before_ma" = "$after_ma" ]; then
    say "integrity: this run left $OPT/tools and $MA exactly as it found them"
else
    say "integrity: WARNING -- THIS RUN changed a watched repo:"
    printf '%s\n%s\n' "$before_tools" "$before_ma" > /tmp/selftest-before.$$
    printf '%s\n%s\n' "$after_tools" "$after_ma" > /tmp/selftest-after.$$
    diff /tmp/selftest-before.$$ /tmp/selftest-after.$$ | sed 's/^/           /' \
        | tee -a "$LOG" || true
    rm -f /tmp/selftest-before.$$ /tmp/selftest-after.$$
    warnings=$((warnings + 1))
fi

say ""
if [ "$rc" -eq 0 ]; then
    say "SELF-TEST PASSED ($warnings container warning(s) above -- see them, they are real)"
else
    say "SELF-TEST FAILED (exit $rc); $warnings container warning(s) above"
fi
say "log: $LOG"

exit "$rc"

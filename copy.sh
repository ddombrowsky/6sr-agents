#!/bin/bash
#
# Sync between this repo and v/, which is bind-mounted to /opt in the container.
#
# Directories that exist on BOTH sides. Listed once so a new one (template_repo_maker
# was the third) is added in a single place instead of in four cp lines that drift.
# Each is copied with `dir/*` AND `dir/.gitignore`: the glob does not match dotfiles,
# and /opt/tools and /opt/master_agent are watched git repos, so a missing .gitignore
# means the first __pycache__ or cache write leaves an untracked file and the next
# monitor cycle prints LIVE TRADING HALTED. scripts/selftest.sh checks for exactly this.
SYNCED_DIRS="master_agent tools template_repo template_repo_forecast template_repo_kalshi template_repo_maker template_repo_null"

# The agent-side files copied into v/agents/ (== /opt/agents in the container). Listed
# once, in a variable, for the same reason SYNCED_DIRS is: the hardcoded cp list this
# replaces silently disabled the ENTIRE emperor self-revision layer for a week.
# memory_tools.py was added to emperor-agent.py's imports on 2026-08-13 and never added
# to that cp list, so from then on every emperor-agent.py invocation died at
# `import memory_tools` -- and the only trace was a 170-byte agent_*.log nobody read,
# while emperor.sh went on logging ordinary-looking cycles. If you add an import to
# emperor-agent.py, add the module here.
#
# memory.json is deliberately NOT in this list: it is state one container writes, not
# code the host deploys. It flows container -> host via the reverse direction's glob and
# never the other way -- see the note in the --to branch below for why not even as a seed.
AGENT_FILES="requirements.txt emperor-agent.py sr_agent_tools.py memory_tools.py tools.json"

# Count every cp that fails and refuse to exit 0 if any did.
#
# This script used to fail SILENTLY and half-deploy. `git init`/`git checkout` run inside
# the container as root rewrites every tracked file in /opt/master_agent as root-owned, at
# which point cp from the host cannot overwrite them -- and because callers routinely run
# `./copy.sh --to >/dev/null 2>&1`, the "Permission denied" lines went nowhere. The
# container then ran a version of a module several edits old while the host looked correct,
# and the symptom was a scoring bug that had already been fixed. Loud is the whole point:
# a partial deploy is worse than a failed one.
_copy_failures=0
say_cp() {
    if ! cp -v "$@" ; then
        _copy_failures=$((_copy_failures + 1))
    fi
}

# copy_files SRC... DEST -- like `cp SRC... DEST` but only for plain files.
#
# Two things a bare `cp dir/*` gets wrong here, both of which used to be counted as
# copy failures or silently produced junk:
#
#   - Subdirectories. /opt/tools and /opt/master_agent are live git repos that grow a
#     __pycache__ (and v/agents/ has its bak.XXXXX/ and venv/), which the glob matches
#     and cp refuses without -r. That is a hard error, not a real problem, and it made
#     the script exit 1 on a perfectly good deploy.
#   - Symlinks. v/ holds monitor.py, strat_manager.py &c as links into master_agent/
#     (created at the bottom of the --to branch). cp DEREFERENCES them, so the reverse
#     direction turned each link into a second, real copy of the module inside scripts/
#     -- duplicates that then drift from the originals they were meant to point at.
#
# Skipping both is right in every direction: the synced dirs are flat by design (see
# the note about flat globs in CLAUDE.md), and nothing that is a symlink in v/ wants
# to become a file on the host.
copy_files() {
    local dest="${!#}"
    local src
    for src in "${@:1:$#-1}" ; do
        [ -e "$src" ] || continue   # unmatched glob left literal
        [ -L "$src" ] && continue   # symlink into master_agent/
        [ -d "$src" ] && continue   # __pycache__, .git, bak.XXXXX, venv
        say_cp "$src" "$dest"
    done
}

if [ "$1" = "--to" ] ; then
    # create.sh calls this against a v/ that has just been made, where none of these
    # exist yet. `mv` of a missing file is an error that used to leave an empty bak.XXXXX
    # behind on every bootstrap, so back up only what is actually there -- and do not
    # make the backup directory at all if there is nothing to put in it.
    mkdir -p ./v/agents
    to_back_up=
    for f in $AGENT_FILES ; do
        [ -e "./v/agents/$f" ] && to_back_up="$to_back_up ./v/agents/$f"
    done
    if [ -n "$to_back_up" ] ; then
        tf=`mktemp -d ./v/agents/bak.XXXXX`
        mv -v $to_back_up $tf
    fi
    for f in $AGENT_FILES ; do
        [ -e "./$f" ] || continue
        say_cp "./$f" ./v/agents/
    done

    # memory_tools.py was missing from that list until 2026-08-21, and emperor-agent.py
    # imports it at line 9. Every emperor self-revision pass on a container that never had
    # it hand-placed died instantly with ModuleNotFoundError -- ssr_agent00 was still in
    # that state when this was found, and the only trace is one line in agent_<stamp>.log,
    # since emperor.sh reports the step as failed and carries on to the next cycle.
    # Exactly the flat-glob trap CLAUDE.md warns about, and why the list is now a variable.
    #
    # memory.json is deliberately NOT deployed with it, not even as a seed onto a
    # container that has none. It is not config or seed data: it is whatever
    # emperor-agent.py's `remember` tool wrote down on ONE container, mirrored here by the
    # reverse direction of this script. Copying it onto a different container hands that
    # container another one's memories -- the tracked copy's only fact is about
    # /opt/agents/swarm/ slot files, which do not exist on every container, so seeding it
    # plants something false as often as not. memory_tools.py needs no file to start:
    # _load() returns {} when it is absent and the first `remember` creates it.
    for d in $SYNCED_DIRS ; do
        [ -d "./$d" ] || continue
        mkdir -p "./v/$d"
        copy_files ./$d/* "./v/$d/"
        [ -e "./$d/.gitignore" ] && say_cp "./$d/.gitignore" "./v/$d/"
    done
    rm -f ./scripts/env.sh
    copy_files ./scripts/*.{sh,py} ./v/

    # monitor.py subprocesses `/opt/strat_manager.py` by absolute path at every call
    # site, and once.sh/st.sh run `python3 monitor.py` from /opt -- but the files live in
    # /opt/master_agent. The working container has had these symlinks since July, made by
    # hand and living only in the gitignored v/, so a container created fresh by create.sh
    # arrives without them and every clone fails with FileNotFoundError. Same class of
    # trap as the missing .gitignore above: infrastructure that exists only in the one
    # container nobody has recreated.
    for f in monitor.py strat_manager.py leaderboard.py live_report.py ; do
        [ -e "./v/master_agent/$f" ] || continue
        ln -sfn "master_agent/$f" "./v/$f" && echo "symlink ./v/$f -> master_agent/$f"
    done
else
    copy_files ./v/agents/* ./
    for d in $SYNCED_DIRS ; do
        [ -d "./v/$d" ] || continue
        mkdir -p "./$d"
        copy_files ./v/$d/* "./$d/"
        [ -e "./v/$d/.gitignore" ] && say_cp "./v/$d/.gitignore" "./$d/"
    done
    copy_files ./v/*.{sh,py} ./scripts/
fi

if [ "$_copy_failures" -gt 0 ] ; then
    echo "" >&2
    echo "ERROR: $_copy_failures cp(s) FAILED -- this is a PARTIAL deploy." >&2
    echo "The usual cause is root-owned files under ./v, left by git or a process" >&2
    echo "running as root inside the container. Fix with:" >&2
    echo "    docker exec \$(cat .containername) chown -R \$(id -u):\$(id -g) /opt" >&2
    echo "then re-run this script and confirm it exits 0." >&2
    exit 1
fi

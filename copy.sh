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
SYNCED_DIRS="master_agent tools template_repo template_repo_forecast template_repo_maker"

if [ "$1" = "--to" ] ; then
    tf=`mktemp -d ./v/agents/bak.XXXXX`
    mv -v ./v/agents/agent-bootstrap.py ./v/agents/sr_agent_tools.py \
        ./v/agents/tools.json $tf
    cp -v requirements.txt agent-bootstrap.py sr_agent_tools.py \
        tools.json ./v/agents/
    for d in $SYNCED_DIRS ; do
        [ -d "./$d" ] || continue
        mkdir -p "./v/$d"
        cp -v ./$d/* "./v/$d/" 2>/dev/null
        [ -e "./$d/.gitignore" ] && cp -v "./$d/.gitignore" "./v/$d/"
    done
    rm -f ./scripts/env.sh
    cp -v ./scripts/*.{sh,py} ./v/

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
    cp -v ./v/agents/* ./ 2>/dev/null
    for d in $SYNCED_DIRS ; do
        [ -d "./v/$d" ] || continue
        mkdir -p "./$d"
        cp -v ./v/$d/* "./$d/" 2>/dev/null
        [ -e "./v/$d/.gitignore" ] && cp -v "./v/$d/.gitignore" "./$d/"
    done
    cp -v ./v/*.{sh,py} ./scripts/ 2>/dev/null
fi

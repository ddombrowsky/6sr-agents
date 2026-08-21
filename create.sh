#!/bin/bash
#
# Create a container AND the /opt volume it runs against, for one domain, ready to start.
#
#     ./create.sh [--reuse-volume] [<domain>]        # domain defaults to sdex
#     docker start $(cat .containername)
#
# Those two lines are the whole contract. Everything the running system needs -- the
# populated v/ tree, the venv with `ollama` in it, the three git repos, env.sh with the
# API key and the DOMAIN selection -- is built here, so that `docker start` brings up a
# system that works on its first cycle instead of one that has to be hand-finished.
#
# Why any of this is necessary: /opt is a bind mount of v/, which is gitignored, so a new
# worktree starts with NO volume at all. The pieces below are exactly the ones that used
# to live only inside the one container nobody had ever recreated:
#
#   * /opt/agents/venv -- emperor.sh probes for an interpreter that can `import ollama`
#     and falls back to /usr/bin/python3, which cannot. Without the venv the whole LLM
#     layer is silently off and the logs look like ordinary cycles.
#   * /opt/master_agent and /opt/tools as git repos -- monitor.py's
#     check_boundary_integrity() halts live trading on a dirty or non-repo watched dir,
#     and emperor.sh commits its own self-revisions into them.
#   * the domain's template repo as a git repo -- every spawn is
#     `git clone file:///opt/template_repo...`, so without a commit in it the population
#     cannot grow at all.
#   * /opt/env.sh -- emperor.sh does `. ./env.sh` before anything else, and copy.sh
#     deliberately does NOT deploy it (it holds the key, and it is where DOMAIN is set).
#
# The container is created STOPPED. It is brought up once, with the emperor kill switch
# (/opt/emperor.sh.UNMANAGED) already in place so supervisor's emperor sits inert, only
# so the venv can be built by the container's own interpreter -- building it on the host
# would pin the host's Python, not the image's. Then it is stopped and the switch removed.
#
# this relies on this file being in the image:
#
# /etc/supervisor/conf.d/emperor.conf
#
# [program:emperor]
# command=/opt/master_agent/emperor.sh
# directory=/opt/master_agent
# autostart=true
# autorestart=true
# startsecs=1
# stdout_logfile=/dev/fd/1
# stdout_logfile_maxbytes=0
# stderr_logfile=/dev/fd/2
# stderr_logfile_maxbytes=0
# stopsignal=TERM
# stopasgroup=true
# killasgroup=true

# -n flag = nodaemon, required to exist as init proc

set -e

CONTAINERNAME=.containername
IMAGE=${AGENT_IMAGE:-agenttest:latest}
REPO=$(cd "$(dirname "$0")" && pwd)

usage() {
    sed -n '3,6p' "$0" | sed 's/^# \{0,1\}//'
}

# ------------------------------------------------------------------- arguments
DOMAIN_NAME=
REUSE_VOLUME=0
for arg in "$@" ; do
    case "$arg" in
        --reuse-volume) REUSE_VOLUME=1 ;;
        -h|--help) usage ; exit 0 ;;
        -*) echo "ERROR: unknown flag $arg" >&2 ; usage >&2 ; exit 1 ;;
        *)
            if [ -n "$DOMAIN_NAME" ] ; then
                echo "ERROR: more than one domain given ($DOMAIN_NAME, $arg)" >&2
                exit 1
            fi
            DOMAIN_NAME=$arg
            ;;
    esac
done
DOMAIN_NAME=${DOMAIN_NAME:-sdex}

cd "$REPO"

# ------------------------------------------------------- 1. can we do this at all
if [ -e $CONTAINERNAME ] ; then
    echo "ERROR: $CONTAINERNAME already exists for this worktree" >&2
    echo "       one container per v/ directory. Remove it (and the container it" >&2
    echo "       names) first, or use another worktree." >&2
    exit 1
fi

DOMAIN_SRC="master_agent/domain_${DOMAIN_NAME}.py"
if [ ! -f "$DOMAIN_SRC" ] ; then
    echo "ERROR: no such domain '$DOMAIN_NAME' -- $DOMAIN_SRC does not exist." >&2
    echo "       available:" >&2
    for f in master_agent/domain_*.py ; do
        n=${f#master_agent/domain_} ; n=${n%.py}
        echo "         $n" >&2
    done
    exit 1
fi

# Which template repo this domain clones its spawns from. Read out of the domain module
# rather than kept in a table here: TEMPLATE_REPO is the source of truth (and the mapping
# is not mechanical -- DOMAIN=sdex_maker clones /opt/template_repo_maker), so a table here
# would be a second copy of it, free to drift. The container re-answers the same question
# from the imported module at the end and the two are compared.
TEMPLATE_URL=$(sed -n "s|^TEMPLATE_REPO *=.*\(file:///opt/template_repo[A-Za-z0-9_]*\).*|\1|p" \
    "$DOMAIN_SRC" | head -1)
if [ -z "$TEMPLATE_URL" ] ; then
    echo "ERROR: could not find a file:///opt/template_repo... TEMPLATE_REPO in" >&2
    echo "       $DOMAIN_SRC. A domain whose seed genome lives somewhere else cannot" >&2
    echo "       be bootstrapped by this script; set it up by hand." >&2
    exit 1
fi
TEMPLATE_DIR=${TEMPLATE_URL#file:///opt/}
if [ ! -d "$TEMPLATE_DIR" ] ; then
    echo "ERROR: domain '$DOMAIN_NAME' seeds from $TEMPLATE_URL, but ./$TEMPLATE_DIR" >&2
    echo "       does not exist in this repo, so there is nothing to git-init and no" >&2
    echo "       strategy could ever be spawned. Either add ./$TEMPLATE_DIR (a main.py" >&2
    echo "       and a config.json is the whole of it) or point the domain's template" >&2
    echo "       override env var at a repo that does exist." >&2
    exit 1
fi

# copy.sh only deploys the directories it knows about. A template that is not in its
# SYNCED_DIRS would be git-inited here and then never updated by a deploy, which is a
# worse failure than refusing now: the container would run a template frozen at creation.
if ! grep -q "SYNCED_DIRS=.*\b${TEMPLATE_DIR}\b" copy.sh ; then
    echo "ERROR: $TEMPLATE_DIR is not in copy.sh's SYNCED_DIRS, so ./copy.sh --to would" >&2
    echo "       never deploy it. Add it there first." >&2
    exit 1
fi

if [ "$REUSE_VOLUME" -eq 0 ] && [ -d v ] && [ -n "$(ls -A v 2>/dev/null)" ] ; then
    echo "ERROR: ./v already exists and is not empty. That is a live volume -- this" >&2
    echo "       script git-inits repos and rewrites env.sh inside it. Move it aside," >&2
    echo "       or pass --reuse-volume if you really mean to bootstrap over it." >&2
    exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1 ; then
    echo "ERROR: image $IMAGE not found. Build it, or set AGENT_IMAGE." >&2
    exit 1
fi

# The key. Taken from the environment if it is already exported, otherwise lifted out of
# the host's gitignored env.sh. Absence is a WARNING, not an error: paper trading and
# culling work fine without a model, they just do not evolve -- which is exactly what
# emperor.sh says about a missing venv, and is a legitimate way to bring a domain up.
API_KEY=${OLLAMA_API_KEY:-}
if [ -z "$API_KEY" ] ; then
    for f in env.sh scripts/env.sh ; do
        [ -f "$f" ] || continue
        API_KEY=$(sed -n 's/^[[:space:]]*export[[:space:]]*OLLAMA_API_KEY=//p' "$f" \
            | tr -d '"'"'" | head -1)
        [ -n "$API_KEY" ] && break
    done
fi
if [ -z "$API_KEY" ] ; then
    echo "WARNING: no OLLAMA_API_KEY found (not in the environment, not in ./env.sh)." >&2
    echo "         v/env.sh will be written with an empty key: the population will run" >&2
    echo "         and be scored, but every revision will fail. Fill it in before start." >&2
fi

# ---------------------------------------------------------- 2. pick a container name
# find the first unused container name ssr_agentNN, starting at NN=00
existing=$(docker ps -a --format '{{.Names}}')
name=
n=0
while [ "$n" -lt 100 ]; do
    candidate=$(printf 'ssr_agent%02d' "$n")
    if ! echo "$existing" | grep -qx "$candidate"; then
        name=$candidate
        break
    fi
    n=$((n + 1))
done

if [ -z "$name" ]; then
    echo "no available container name (ssr_agent00-ssr_agent99 all taken)" >&2
    exit 1
fi

echo "domain:    $DOMAIN_NAME  (seeds from $TEMPLATE_URL)"
echo "container: $name"
echo "image:     $IMAGE"
echo ""

# Nothing is torn down automatically on failure. A half-built volume with a stopped
# container beside it is debuggable; a script that deletes both leaves nothing to look at,
# and `docker rm -f` on the wrong name because a variable was empty is unrecoverable.
CONTAINER_CREATED=0
on_error() {
    echo "" >&2
    echo "create.sh FAILED. Nothing was removed. To start over:" >&2
    [ "$CONTAINER_CREATED" -eq 1 ] && echo "    docker rm -f $name" >&2
    echo "    rm -f $CONTAINERNAME" >&2
    echo "    rm -rf ./v          # only if you are sure it holds nothing you want" >&2
}
trap on_error ERR

# --------------------------------------------------------- 3. lay out the volume
echo "--- laying out ./v"
mkdir -p v v/agents v/strategies v/trades v/emperor_logs

# The kill switch goes in BEFORE the container is ever started, so supervisor's emperor
# comes up inert and cannot run a cycle against a volume that has no venv, no repos and
# no env.sh yet. Removed at the very end, after the container is stopped again.
touch v/emperor.sh.UNMANAGED

echo "--- ./copy.sh --to"
./copy.sh --to

# ------------------------------------------------------------------ 4. env.sh
# Not deployed by copy.sh on purpose (it holds the key, and copy.sh --to explicitly
# removes scripts/env.sh before deploying scripts/). This is the only place DOMAIN is
# set for the running system, so it is what makes `create.sh <domain>` mean anything.
echo "--- writing v/env.sh (DOMAIN=$DOMAIN_NAME)"
cat > v/env.sh <<ENV_EOF
# source this file
#
# Written by create.sh. Not tracked, not deployed by copy.sh.

. /opt/agents/venv/bin/activate

export COLORTERM=
export CLAUDE_CODE_DISABLE_MOUSE=1
export OLLAMA_API_KEY=$API_KEY
export DOMAIN=$DOMAIN_NAME
ENV_EOF
chmod 600 v/env.sh

# ------------------------------------------------------------- 5. the git repos
# Done on the host, not in the container: git here already has an identity, this repo's
# own history is the source of the differential baseline below, and a .git created by root
# inside the container is the exact thing that makes a later `copy.sh --to` half-deploy.
GIT_ID=(-c user.name=Bootstrapping\ Bot -c user.email=bot@example.com)

# init_repo <dir> <watched|seed>
#
# The .gitignore check is fatal for a WATCHED repo and a warning for a SEED one, because
# the consequence differs by a lot. /opt/master_agent and /opt/tools are watched by
# monitor.py's check_boundary_integrity(): one untracked __pycache__ there and the next
# cycle prints LIVE TRADING HALTED. Both directions of copy.sh use `cp dir/*`, which does
# not match dotfiles, so the .gitignore is deployed by name -- and a template repo that
# arrived without one only means its clones commit their own state.json, which is untidy
# and harmless. scripts/selftest.sh checks the same thing from the other side.
init_repo() {
    local dir="$1" kind="$2"
    if [ -d "$dir/.git" ] ; then
        echo "    $dir already a git repo -- left alone"
        return 0
    fi
    if [ ! -f "$dir/.gitignore" ] ; then
        if [ "$kind" = watched ] ; then
            echo "ERROR: $dir has no .gitignore. The first __pycache__ or cache write" >&2
            echo "       there would leave an untracked file and halt live trading. Add" >&2
            echo "       one to the matching directory in this repo and re-run." >&2
            return 1
        fi
        echo "WARNING: $dir has no .gitignore, so every clone of it will commit its own" >&2
        echo "         state.json and __pycache__. Untidy, not fatal." >&2
    fi
    git "${GIT_ID[@]}" -C "$dir" init -q
    git "${GIT_ID[@]}" -C "$dir" add -A
    git "${GIT_ID[@]}" -C "$dir" commit -q -m "initial import by create.sh ($DOMAIN_NAME)"
    echo "    $dir  $(git -C "$dir" rev-parse --short HEAD)"
}

echo "--- git repos"
init_repo v/master_agent watched
init_repo v/tools watched
init_repo "v/$TEMPLATE_DIR" seed

# The differential baseline for selftest_domain.py. Its search is content-addressed --
# the newest commit of monitor.py that still defines the three functions that moved into
# domain_sdex -- and the repo just created has a one-commit history, so that search finds
# nothing and EVERY differential check silently skips. A green run that verified nothing
# is worse than a red one, so scripts/selftest.sh looks for a `domain-baseline` branch as
# its fallback and this is what puts one there, imported from the mirror's history.
#
# Built with hash-object/mktree/commit-tree rather than a checkout: an orphan branch made
# by `git checkout --orphan` would rewrite the working tree of a repo the container is
# about to run out of.
echo "--- domain-baseline branch"
MARKERS=('def _config_is_sane(' 'def fetch_marks_for_cycle(' 'def apply_seed_thresholds(')
baseline_commit=
for c in $(git -C "$REPO" log --format=%H -n200 -- master_agent/monitor.py) ; do
    blob=$(git -C "$REPO" show "$c:master_agent/monitor.py" 2>/dev/null) || continue
    ok=1
    for m in "${MARKERS[@]}" ; do
        case "$blob" in *"$m"*) ;; *) ok=0 ; break ;; esac
    done
    [ "$ok" -eq 1 ] && { baseline_commit=$c ; break ; }
done
if [ -z "$baseline_commit" ] ; then
    echo "WARNING: no pre-refactor monitor.py in this repo's history (searched for" >&2
    echo "         ${MARKERS[*]}). /opt/selftest.sh will report that it could not find a" >&2
    echo "         baseline; every differential check there will skip." >&2
elif git -C v/master_agent rev-parse --verify -q domain-baseline >/dev/null ; then
    echo "    domain-baseline already exists -- left alone"
else
    # All three files selftest_domain.py reads out of the baseline commit, not just
    # monitor.py: it also loads master-agent.py (for the revision-prompt differential --
    # the file has a hyphen and cannot be imported, so it is read as a blob) and
    # tools.json. Missing either only costs those sections, so they are optional here;
    # monitor.py is the one that must be there.
    tree_input=
    for f in monitor.py master-agent.py tools.json ; do
        git -C "$REPO" cat-file -e "$baseline_commit:master_agent/$f" 2>/dev/null || continue
        b=$(git -C "$REPO" show "$baseline_commit:master_agent/$f" \
            | git -C v/master_agent hash-object -w --stdin)
        tree_input="${tree_input}100644 blob $b	$f
"
    done
    t=$(printf '%s' "$tree_input" | git -C v/master_agent mktree)
    c=$(GIT_AUTHOR_NAME='Bootstrapping Bot' GIT_AUTHOR_EMAIL=bot@example.com \
        GIT_COMMITTER_NAME='Bootstrapping Bot' GIT_COMMITTER_EMAIL=bot@example.com \
        git -C v/master_agent commit-tree "$t" \
            -m "pre-refactor baseline, imported from mirror ${baseline_commit:0:12}")
    git -C v/master_agent branch domain-baseline "$c"
    echo "    domain-baseline -> ${c:0:12} (mirror ${baseline_commit:0:12})"
fi

# ------------------------------------------------------------- 6. the container
echo ""
echo "--- starting container $name (emperor inert: v/emperor.sh.UNMANAGED)"
docker run -d --name "$name" \
    --volume "$REPO/v:/opt" \
    "$IMAGE" \
    supervisord -c /etc/supervisor/supervisord.conf -n >/dev/null
CONTAINER_CREATED=1
echo "$name" > $CONTAINERNAME

# Everything in /opt is owned by the host user, and everything in the container runs as
# root. Without this git refuses every one of those repos as "dubious ownership", which
# takes out check_boundary_integrity, emperor.sh's self-commits and every strategy clone
# at once. Stored in the container's own filesystem, so it survives start/stop.
docker exec "$name" git config --global --add safe.directory '*'

echo "--- building /opt/agents/venv with the image's interpreter (this takes a while)"
docker exec "$name" sh -c '
    set -e
    python3 -m venv /opt/agents/venv
    /opt/agents/venv/bin/pip install --quiet --upgrade pip
    /opt/agents/venv/bin/pip install --quiet -r /opt/agents/requirements.txt
'

# The venv was created by root. Hand /opt back to the invoking user or the next
# `./copy.sh --to` half-deploys against root-owned files -- silently, because callers
# routinely redirect it to /dev/null.
docker exec "$name" chown -R "$(id -u):$(id -g)" /opt

# ------------------------------------------------------------ 7. prove it works
echo "--- verifying"
docker exec "$name" /opt/agents/venv/bin/python -c 'import ollama' \
    || { echo "ERROR: the venv cannot import ollama" >&2 ; false ; }
echo "    venv can import ollama"

container_template=$(docker exec -e "DOMAIN=$DOMAIN_NAME" -w /opt/master_agent "$name" \
    /opt/agents/venv/bin/python -c "
import sys
sys.path.insert(0, '/opt/tools')
import domain
mod = domain.get()
problems = domain.check(mod)
for p in problems:
    print('CONTRACT ' + p, file=sys.stderr)
sys.exit(1) if problems else print(mod.TEMPLATE_REPO)
")
echo "    domain_$DOMAIN_NAME imports and satisfies the contract"

if [ "$container_template" != "$TEMPLATE_URL" ] ; then
    echo "ERROR: the domain module in the container seeds from $container_template," >&2
    echo "       but this script git-inited $TEMPLATE_URL. The template override env" >&2
    echo "       var is probably set, or v/ is out of date." >&2
    false
fi
echo "    seed genome $TEMPLATE_URL is a git repo with a commit"

# --------------------------------------------------------------- 8. leave it stopped
echo ""
echo "--- stopping container (the emperor kill switch is removed after the stop, so"
echo "    it never gets a chance to run a cycle before you ask for one)"
docker stop "$name" >/dev/null
rm -f v/emperor.sh.UNMANAGED

trap - ERR
echo ""
echo "$name is ready. Start it with:"
echo "    docker start $name"
echo ""
echo "To bring it up inert instead (emperor does nothing until you 'rm' the file):"
echo "    touch v/emperor.sh.UNMANAGED && docker start $name"

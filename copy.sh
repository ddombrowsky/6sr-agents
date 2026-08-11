#!/bin/sh
#

if [ "$1" = "--to" ] ; then
    tf=`mktemp -d ./v/agents/bak.XXXXX`
    mv -v ./v/agents/agent-bootstrap.py ./v/agents/sr_agent_tools.py \
        ./v/agents/tools.json $tf
    cp -v requirements.txt agent-bootstrap.py sr_agent_tools.py \
        tools.json ./v/agents/
    cp -v ./master_agent/* ./v/master_agent/
    cp -v ./tools/* ./v/tools/
    cp -v ./template_repo/* ./v/template_repo/
    cp -v ./template_repo_kalshi/* ./v/template_repo_kalshi/
    rm -f ./scripts/env.sh
    cp -v ./scripts/*.{sh,py} ./v/
else
    cp -v ./v/agents/* ./ 2>/dev/null
    cp -v ./v/master_agent/* ./master_agent/ 2>/dev/null
    cp -v ./v/tools/* ./tools/ 2>/dev/null
    cp -v ./v/template_repo/* ./template_repo/ 2>/dev/null
    cp -v ./v/template_repo_forecast/* ./template_repo_forecast/ 2>/dev/null
    cp -v ./v/template_repo_kalshi/* ./template_repo_kalshi/ 2>/dev/null
    cp -v ./v/*.{sh,py} ./scripts/ 2>/dev/null
fi

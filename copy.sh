#!/bin/sh
#

if [ "$1" == "--to" ] ; then
    tf=`mktemp -d ./v/agents/bak.XXXXX`
    mv -v ./v/agents/agent-bootstrap.py ./v/agents/sr_agent_tools.py \
        ./v/agents/tools.json $tf
    cp -v requirements.txt agent-bootstrap.py sr_agent_tools.py \
        tools.json ./v/agents/
else
    cp -v ./v/agents/* ./ 2>/dev/null
    cp -v ./v/master_agent/* ./master_agent/ 2>/dev/null
    cp -v ./v/tools/* ./tools/ 2>/dev/null
    cp -v ./v/template_repo/* ./template_repo/ 2>/dev/null
fi

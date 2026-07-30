#!/bin/sh
#

tf=`mktemp -d ./v/agents/bak.XXXXX`
mv -v ./v/agents/agent-bootstrap.py ./v/agents/sr_agent_tools.py \
    ./v/agents/tools.json $tf
cp -v requirements.txt agent-bootstrap.py sr_agent_tools.py \
    tools.json ./v/agents/

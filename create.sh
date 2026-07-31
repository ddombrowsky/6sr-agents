#!/bin/sh
#

# TODO: this should come from the agenttest:latest image and use
# something supervisord or --init, plus /opt/master_agent/emperor.sh
docker run -d --init --name agenttest \
    --volume ./v:/opt \
    agenttest:latest \
    supervisord -c /etc/supervisor/supervisord.conf -n

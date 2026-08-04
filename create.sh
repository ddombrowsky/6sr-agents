#!/bin/sh
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

docker run -d --name agenttest \
    --volume ./v:/opt \
    agenttest:latest \
    supervisord -c /etc/supervisor/supervisord.conf -n

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

echo "starting container $name"

docker run -d --name "$name" \
    --volume ./v:/opt \
    agenttest:latest \
    supervisord -c /etc/supervisor/supervisord.conf -n

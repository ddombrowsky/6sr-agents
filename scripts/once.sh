#!/bin/bash

# In lieu of the emporor.sh script, do one loop of the monitor,
# capturing the log, and exit.
# The difference between this and the `emperor.sh --once` call
# is that this doesn't do any of the emperor revision stuff.

DSTR=`date +%s`
LOG=/opt/emperor_logs/monitor-once-$DSTR.log

echo starting market recorder...
python monitor.py --ensure-recorder

# comment this out to use ollama if you have any credits left
export MASTER_AGENT_MODEL=granite

python -u monitor.py > $LOG 2>&1 &

sleep 5

touch /opt/.monitor.py.exit

echo Logging to $LOG
echo waiting for exit...
wait

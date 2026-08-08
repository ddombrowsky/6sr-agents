#!/bin/bash

# In lieu of the emporor.sh script, do one loop of the monitor,
# capturing the log, and exit.
# The difference between this and the `emperor.sh --once` call
# is that this doesn't do any of the emperor revision stuff.

. env.sh

DSTR=`date +%s`
LOG=/opt/emperor_logs/monitor-once-$DSTR.log

echo starting market recorder...
python3 monitor.py --ensure-recorder

# comment this out to use ollama if you have any credits left
export MASTER_AGENT_MODEL=granite

date > $LOG
touch /opt/.monitor.py.exit
python3 -u monitor.py 2>&1 | tee $LOG &

echo Logging to $LOG
echo waiting for exit...
wait

echo === Monitor Exited === >> $LOG
date >> $LOG

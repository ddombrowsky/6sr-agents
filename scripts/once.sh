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

export CYCLE_SLEEP=14400
# comment this out to use ollama if you have any credits left -- irrelevant while
# REVISION_MODE=manual below, since that path never calls the model, but left set so
# switching back to auto mode doesn't silently start spending Ollama cloud credits again
#export MASTER_AGENT_MODEL=qwen

# Revise by hand through Claude Code instead of the local model: monitor.py dumps the
# revision prompt to /opt/emperor_logs/pending_revision.md and blocks the whole cycle
# until `master-agent.py revision-done <name>` is run. See v/strategies/CLAUDE.md for
# the human-side workflow. Comment out to go back to the local Ollama model.
#export REVISION_MODE=manual

date > $LOG
#touch /opt/.monitor.py.exit
# Piped through tee rather than redirected straight to $LOG: under REVISION_MODE=manual
# the cycle can pause and print a banner asking for action (what to read, what command
# to run to resume) -- that has to reach this terminal, not just the log file, or
# there'd be nothing here telling you to go do it.
python3 -u monitor.py 2>&1 | tee -a $LOG &

echo Logging to $LOG
echo waiting for exit...
wait

echo === Monitor Exited === >> $LOG
date >> $LOG

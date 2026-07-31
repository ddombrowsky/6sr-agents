#!/bin/sh

# Emperor script (higher than master)

# 1. Run the monitor.py script for 6 hours, capture the log
# 2. Use the agents/agent-bootstrap.py agent to analyze the
#    log file and SYSTEM_STATE.md, and them make changes
#    to the following files:
#    * master_agent/master-agent.py
#    * master_agent/monitor.py
#    * master_agent/sr_agent_tools.py
#    * master_agent/strat_manager.py
# 3. Analyze the current overall system and write it
#    to SYSTEM_STATE.md

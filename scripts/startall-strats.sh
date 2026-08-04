#!/bin/bash

# Start every strategy still in the database.
# This can be dangerous and lead to a lot of rate limiting, so
# it is not part of the main system and is considered dev only.

for s in `./leaderboard.py | grep stopped | grep -v N/A | awk '{print $1}'` ; do
    ./strat_manager.py start $s
    sleep $((RANDOM % 45))
done

#!/usr/bin/env python3
"""Utility to log simulated trades for agents.
Each agent gets its own log file under /opt/trades/<agent_name>.log.
Log format: JSON line with fields: timestamp, agent, action, price, amount_usd, amount_xlm, balance_usd, balance_xlm
"""
import json
import os
import time
from pathlib import Path

BASE_DIR = Path('/opt/trades')
BASE_DIR.mkdir(parents=True, exist_ok=True)

def record_trade(agent_name, action, price, amount_usd, amount_xlm, balance_usd, balance_xlm):
    entry = {
        "timestamp": time.time(),
        "agent": agent_name,
        "action": action,  # "buy" or "sell"
        "price": price,
        "amount_usd": amount_usd,
        "amount_xlm": amount_xlm,
        "balance_usd": balance_usd,
        "balance_xlm": balance_xlm,
    }
    log_path = BASE_DIR / f"{agent_name}.log"
    with open(log_path, 'a') as f:
        f.write(json.dumps(entry) + "\n")

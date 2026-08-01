#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

# Add tools directory to path
sys.path.append('/opt/tools')
from price_feed import get_price
from trade_logger import execute_trade

# Load configuration
CONFIG_PATH = Path('config.json')
STATE_PATH = Path('state.json')

if not CONFIG_PATH.exists():
    print('Missing config.json')
    sys.exit(1)

with open(CONFIG_PATH) as f:
    config = json.load(f)

agent_name = config.get('name', 'unnamed')
buy_below = config.get('buy_below')  # price threshold to buy (USD)
sell_above = config.get('sell_above')  # price threshold to sell (USD)
trade_amount_usd = config.get('trade_amount_usd', 10)  # How much USD to use per trade

# Load or initialize state
if STATE_PATH.exists():
    with open(STATE_PATH) as f:
        state = json.load(f)
else:
    # start with 1000 USD, 0 XLM
    state = {'balance_usd': 1000.0, 'balance_xlm': 0.0}

print(f"Agent {agent_name} starting with USD {state['balance_usd']:.2f}, XLM {state['balance_xlm']:.4f}")

while True:
    price = get_price()
    if price is None:
        time.sleep(30)
        continue

    # Decide: side/action/requested_usd, or None if no trade this tick. Execution
    # (balance mutation, clamping, logging, live submission) is handled by
    # execute_trade in trade_logger.py -- this is the only part a strategy revision
    # should need to touch.
    side = action = requested_usd = None
    if price <= buy_below:
        side = action = 'buy'
        requested_usd = trade_amount_usd
    elif price >= sell_above:
        side = action = 'sell'
        requested_usd = trade_amount_usd

    if side is not None:
        state = execute_trade(agent_name, action, side, price, requested_usd, state)

    # Save state
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f)
    # Wait before next check (30 seconds)
    time.sleep(30)

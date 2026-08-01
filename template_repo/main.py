#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

# Add tools directory to path
sys.path.append('/opt/tools')
from price_feed import get_price
from trade_logger import record_trade
from stellar_trader import submit_trade

# Rollout phase 3 (pubnet-plan.md): monitor.py may mark this strategy live via
# live.flag. When live, submit_trade() fires for real, in addition to the paper
# math below — its result is logged separately (stellar_trader.py writes
# <name>.pubnet.log) and never written back into balance_usd/balance_xlm, which stay
# the ranking signal regardless of live status.
LIVE_FLAG = Path('live.flag')

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
    # Decision making — paper math always runs, live or not (pubnet-plan.md); it's
    # what feeds state.json/net-worth ranking regardless of live status.
    is_live = LIVE_FLAG.exists()
    if price <= buy_below and state['balance_usd'] >= trade_amount_usd:
        # Buy XLM with trade_amount_usd
        amount_xlm = trade_amount_usd / price
        state['balance_usd'] -= trade_amount_usd
        state['balance_xlm'] += amount_xlm
        record_trade(agent_name, 'buy', price, trade_amount_usd, amount_xlm, state['balance_usd'], state['balance_xlm'])
        print(f"[{agent_name}] Bought {amount_xlm:.4f} XLM at ${price:.4f}")
        if is_live:
            result = submit_trade('buy', trade_amount_usd)
            print(f"[{agent_name}] LIVE: submit_trade('buy', {trade_amount_usd}) -> {result}")
    elif price >= sell_above and state['balance_xlm'] > 0:
        # Sell all XLM (or a portion)
        amount_xlm = min(state['balance_xlm'], trade_amount_usd / price)  # sell up to trade_amount_usd worth
        usd_gained = amount_xlm * price
        state['balance_xlm'] -= amount_xlm
        state['balance_usd'] += usd_gained
        record_trade(agent_name, 'sell', price, usd_gained, amount_xlm, state['balance_usd'], state['balance_xlm'])
        print(f"[{agent_name}] Sold {amount_xlm:.4f} XLM at ${price:.4f}")
        if is_live:
            result = submit_trade('sell', usd_gained)
            print(f"[{agent_name}] LIVE: submit_trade('sell', {usd_gained:.4f}) -> {result}")
    # Save state
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f)
    # Wait before next check (30 seconds)
    time.sleep(30)

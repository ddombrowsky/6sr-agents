#!/usr/bin/env python3
"""Utility to log simulated trades for agents.
Each agent gets its own log file under /opt/trades/<agent_name>.log.
Log format: JSON line with fields: timestamp, agent, action, price, amount_usd, amount_xlm, balance_usd, balance_xlm
"""
import json
import os
import time
from pathlib import Path

from stellar_trader import submit_trade

BASE_DIR = Path('/opt/trades')
BASE_DIR.mkdir(parents=True, exist_ok=True)

LIVE_FLAG = Path('live.flag')

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


def execute_trade(agent_name, action, side, price, requested_usd, state, *, is_live=None):
    """Mutate `state` for one trade, log it, and submit it live if this strategy is live.

    This is the single place strategy execution mechanics live, so that a strategy's
    main.py only needs to decide *whether* to trade, not how to safely mutate balances,
    log the result, or gate/submit a real order. `main.py` should call this instead of
    reimplementing any of it.

    agent_name: used for the log filename (same as record_trade).
    action: free-form label for the log (e.g. 'buy', 'sell', 'sell_stoploss') -- never
        used for control flow, just passed through to record_trade.
    side: normalized 'buy' | 'sell' -- the only thing used to decide which balance to
        mutate and what to pass to stellar_trader.submit_trade.
    price: current price, used to size the trade.
    requested_usd: how much USD notional the caller wants to trade -- a request, not a
        guarantee. Clamped here to what's actually affordable/sellable, so a decide step
        can never overdraft USD or oversell XLM no matter what it asks for.
    state: the {'balance_usd', 'balance_xlm'} dict the caller's loop threads through;
        mutated in place and returned.
    is_live: if None (the normal case), determined by checking for a live.flag file in
        the current working directory -- callers (main.py) run with cwd set to their own
        strategy directory, so this resolves correctly without the caller needing to
        check it itself. Pass explicitly to override (e.g. for tests).

    No-ops (returns state unchanged, no log line, no live submission) if the clamped
    trade size is <= 0.
    """
    if side == 'buy':
        actual_usd = min(requested_usd, state['balance_usd'])
        if actual_usd <= 0:
            return state
        amount_xlm = actual_usd / price
        state['balance_usd'] -= actual_usd
        state['balance_xlm'] += amount_xlm
        record_trade(agent_name, action, price, actual_usd, amount_xlm, state['balance_usd'], state['balance_xlm'])
        print(f"[{agent_name}] Bought {amount_xlm:.4f} XLM at ${price:.4f}")
        trade_usd = actual_usd
    elif side == 'sell':
        actual_xlm = min(requested_usd / price, state['balance_xlm'])
        if actual_xlm <= 0:
            return state
        usd_gained = actual_xlm * price
        state['balance_xlm'] -= actual_xlm
        state['balance_usd'] += usd_gained
        record_trade(agent_name, action, price, usd_gained, actual_xlm, state['balance_usd'], state['balance_xlm'])
        print(f"[{agent_name}] Sold {actual_xlm:.4f} XLM at ${price:.4f}")
        trade_usd = usd_gained
    else:
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    if is_live is None:
        is_live = LIVE_FLAG.exists()
    if is_live:
        result = submit_trade(side, trade_usd)
        print(f"[{agent_name}] LIVE: submit_trade({side!r}, {trade_usd}) -> {result}")

    return state

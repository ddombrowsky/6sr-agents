"""Compute each strategy's score: paper USD weighted 10x over paper XLM's USD
value (divided by 100), so strategies that convert trades into realized USD
rank above ones just sitting on paper XLM that happens to have appreciated.
"""
import json
from pathlib import Path


def compute_score(balance_usd, balance_xlm, price):
    return balance_usd * 10 + (balance_xlm * price) / 100


def score_from_strategy_path(strategy_path, price):
    """Read <strategy_path>/state.json and return its score, or None if unreadable."""
    state_path = Path(strategy_path) / 'state.json'
    if not state_path.exists():
        return None
    try:
        data = json.load(state_path.open())
        usd = data.get('balance_usd', 0.0)
        xlm = data.get('balance_xlm', 0.0)
        return compute_score(usd, xlm, price)
    except Exception:
        return None

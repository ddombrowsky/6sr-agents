"""Compute each strategy's score: mark-to-market net worth, with a small haircut
applied to the XLM leg so that at equal net worth a strategy sitting on realized
USD ranks above one sitting on unrealized XLM.

History: this used to be `balance_usd * 10 + (balance_xlm * price) / 100`, which
made $1 of cash worth 1000x $1 of XLM. The intent (prefer realized gains) was
sound but the weighting dominated everything else: on 2026-08-01, 45 clones that
had never placed a single trade all scored exactly 10000.00 and occupied every
top slot, while the only strategy actually up on net worth (+1.4%) scored 53.80
and ranked ~60th out of 73. The evolutionary loop was therefore culling its best
performers and cloning ones that don't trade. The haircut below preserves the
"prefer realized USD" tie-break without inverting the ranking.
"""
import json
from pathlib import Path

# $1 of XLM counts as UNREALIZED_HAIRCUT until it's sold: a tie-break nudge toward realizing gains,
# and nothing more. Keep this very close to 1.0. A haircut is a hurdle rate -- at 0.97 a
# fully-invested strategy has to be up >3% just to draw level with one sitting in cash,
# which reproduces the original bug in miniature (checked against live balances: the best
# real performer, +1.7% on net worth, scored 986.90 and still ranked below every
# never-traded clone at 1000.00). At 0.999 the hurdle is 0.1% and ranking tracks profit.
UNREALIZED_HAIRCUT = 0.899


def compute_score(balance_usd, balance_xlm, price):
    return balance_usd + balance_xlm * price * UNREALIZED_HAIRCUT


def score_from_strategy_path(strategy_path, price):
    """Read <strategy_path>/state.json and return its score, or None if unreadable."""
    balances = balances_from_strategy_path(strategy_path)
    if balances is None:
        return None
    usd, xlm = balances
    return compute_score(usd, xlm, price)


def balances_from_strategy_path(strategy_path):
    """Return (balance_usd, balance_xlm) from <strategy_path>/state.json, or None.

    Split out from score_from_strategy_path so callers that need the raw components
    (e.g. monitor.py's tie-breaking, which wants to know whether a strategy has
    actually deployed any capital) don't have to re-read and re-parse the file.
    """
    state_path = Path(strategy_path) / 'state.json'
    if not state_path.exists():
        return None
    try:
        data = json.load(state_path.open())
        return float(data.get('balance_usd', 0.0)), float(data.get('balance_xlm', 0.0))
    except Exception:
        return None

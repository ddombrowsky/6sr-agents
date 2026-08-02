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
import sys
from pathlib import Path

if '/opt/tools' not in sys.path:
    sys.path.append('/opt/tools')

try:
    import assets as _assets
    import portfolio as _portfolio
except Exception:      # pragma: no cover - keeps the XLM-only path working standalone
    _assets = _portfolio = None

# $1 of XLM counts as UNREALIZED_HAIRCUT until it's sold: a tie-break nudge toward realizing gains,
# and nothing more. Keep this very close to 1.0. A haircut is a hurdle rate -- at 0.97 a
# fully-invested strategy has to be up >3% just to draw level with one sitting in cash,
# which reproduces the original bug in miniature (checked against live balances: the best
# real performer, +1.7% on net worth, scored 986.90 and still ranked below every
# never-traded clone at 1000.00). At 0.999 the hurdle is 0.1% and ranking tracks profit.
#
# This constant sat at 0.899 while the comment above argued for 0.999 and
# master-agent.py's REVISION_SYSTEM_PROMPT told the model it was 0.999 -- so the
# documented objective and the enforced one disagreed by two orders of magnitude in the
# hurdle. Re-confirmed against live balances before the fix: clone_ae1be2a2d00a held
# 5829.27 XLM at $0.1735615 = $1011.75, genuinely up +1.2% on net worth, and scored
# 909.55 -- 7th, below three never-traded clones sitting in cash at exactly 1000.00.
# The evolutionary loop was again cloning strategies that don't trade.
#
# Do not "harmonize" this with ILLIQUID_HAIRCUT. They are different kinds of number:
# this one is a hurdle rate on an asset the strategy is *expected* to hold and must stay
# near 1.0; that one is a slippage estimate on a thin order book and is supposed to bias
# against exotic legs. The prompt interpolates this value rather than restating it, so
# the two cannot drift apart again.
UNREALIZED_HAIRCUT = 0.999

# Applied to non-XLM legs on top of their mark. This is a DIFFERENT KIND OF NUMBER from
# UNREALIZED_HAIRCUT and the two must never be "harmonized". UNREALIZED_HAIRCUT is a
# hurdle rate on an asset the strategy is expected to hold, so it stays near 1.0.
# ILLIQUID_HAIRCUT is a slippage estimate on a thin order book, and it is *supposed* to
# bias against exotic legs: a discovered asset with a few thousand dollars of depth
# genuinely cannot be exited at its quoted price the way XLM can. dex_price.mark_value
# already caps a position at real bid depth; this is the residual haircut on what's
# left, covering the gap between "there are bids" and "those bids survive contact".
ILLIQUID_HAIRCUT = 0.90

# How long a last-known-good mark may be reused before the leg is valued at zero.
# Deliberately finite: an asset that suddenly cannot be priced is what a rug looks like,
# and carrying its last good mark indefinitely would rank holding a rug above holding
# cash. Six hours is long enough to ride out a Horizon outage and short enough that a
# dead asset stops flattering its strategy within the same day.
STALE_MARK_MAX_AGE = 6 * 3600


def compute_score(balance_usd, balance_xlm, price):
    """XLM-only score. Unchanged signature and meaning -- leaderboard.py, the smoke
    test and any older caller keep using this."""
    return balance_usd + balance_xlm * price * UNREALIZED_HAIRCUT


def compute_score_multi(state, marks):
    """Score a multi-asset state. Returns (score, unpriced_specs).

    `marks` maps spec -> USD unit price (see dex_price.get_marks). USD is counted at
    face, the XLM leg takes UNREALIZED_HAIRCUT, and every other leg takes both its mark
    and ILLIQUID_HAIRCUT.

    A leg with no usable mark contributes **zero** and is reported in `unpriced_specs`
    so callers can surface it as an outage rather than as a mysterious score drop.
    """
    if _portfolio is None:
        usd, xlm = float(state.get('balance_usd', 0.0)), float(state.get('balance_xlm', 0.0))
        price = (marks or {}).get('XLM')
        return (compute_score(usd, xlm, price) if price else usd), []

    state = _portfolio.normalize_state(state)
    marks = marks or {}
    total = state['balance_usd']
    unpriced = []

    for spec, position in state['positions'].items():
        amount = float(position.get('amount') or 0.0)
        if amount <= 0:
            continue
        mark = marks.get(spec)
        if mark is None or mark <= 0:
            unpriced.append(spec)
            continue
        haircut = UNREALIZED_HAIRCUT if spec == _assets.NATIVE else ILLIQUID_HAIRCUT
        total += amount * mark * haircut

    return total, unpriced


def score_from_strategy_path(strategy_path, price, marks=None):
    """Read <strategy_path>/state.json and return its score, or None if unreadable.

    With `marks` omitted this is exactly the old XLM-only behavior, so existing callers
    are unaffected. Pass `marks` (from dex_price.get_marks) to score every leg.

    Never writes back: a running strategy rewrites its own state.json every 30s, and
    normalizing on disk from here would race it.
    """
    state_path = Path(strategy_path) / 'state.json'
    if not state_path.exists():
        return None
    try:
        data = json.load(state_path.open())
    except Exception:
        return None

    if marks is None:
        balances = balances_from_strategy_path(strategy_path)
        if balances is None:
            return None
        usd, xlm = balances
        return compute_score(usd, xlm, price)

    marks = dict(marks)
    marks.setdefault('XLM', price)
    return compute_score_multi(data, marks)[0]


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

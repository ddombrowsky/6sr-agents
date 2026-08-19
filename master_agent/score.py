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
# The prompt interpolates this value rather than restating it, so the two cannot drift
# apart again.
#
# ILLIQUID_HAIRCUT (0.99) used to sit here beside it, a residual slippage nudge on a
# non-XLM leg whose value had already been depth-capped against the real bid ladder. It
# went with the extra-asset stack on 2026-08-13, along with a STALE_MARK_MAX_AGE that
# turned out to have had no readers at all. The distinction the removed comment was at
# pains to draw -- a hurdle rate on an expected holding is not a slippage estimate on a
# thin book -- is worth keeping in mind if a second asset ever comes back: both constants
# were originally set an order of magnitude too punitive, and both times the effect was
# the loop culling the behaviour it had just been extended to explore.
UNREALIZED_HAIRCUT = 0.999


def compute_score(balance_usd, balance_xlm, price):
    """XLM-only score. Unchanged signature and meaning -- leaderboard.py, the smoke
    test and any older caller keep using this."""
    return balance_usd + balance_xlm * price * UNREALIZED_HAIRCUT


def compute_score_multi(state, marks):
    """Score a state's XLM leg and cash. Returns (score, unpriced_specs).

    `marks` maps spec -> USD unit price, or the depth-carrying dict portfolio understands.
    USD is counted at face and the XLM leg takes UNREALIZED_HAIRCUT.

    A leg with no usable mark contributes **zero** and is reported in `unpriced_specs`
    so callers can surface it as an outage rather than as a mysterious score drop.

    The `_multi` in the name is now historical -- XLM is the only leg this domain trades.
    It is kept because domain_sdex.score, selftest_domain and score_from_strategy_path all
    call it by that name, and because a non-XLM position surviving in some old state.json
    still has to be handled rather than silently valued. Such a position is skipped and
    reported unpriced: nothing can open one any more (trade_logger refuses an undeclared
    asset and no config can declare one), so counting it would be marking a balance the
    strategy has no way to have acquired legitimately. Three stopped strategies held one
    at removal time -- two in AQUA, one in USDC -- and each drops to its cash balance.
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
        mark = marks.get(spec) if spec == _assets.NATIVE else None
        if mark is None:
            unpriced.append(spec)
            continue
        # realizable_value depth-caps against the bid ladder when one was supplied, and
        # falls back to amount x unit price when it wasn't -- so a caller that passes
        # plain floats still gets sensible (if less precise) numbers.
        value = _portfolio.realizable_value(amount, mark)
        if value is None:
            unpriced.append(spec)
            continue
        total += value * UNREALIZED_HAIRCUT

    # Short liability (SHORTING_PLAN.md): borrowed_xlm is a debt, not a position, so it
    # never appears in `positions` -- it must be subtracted separately or a strategy that
    # never covers would look like it kept the sale proceeds for free. The buy-back costs
    # slightly *more*, not less, hence dividing by UNREALIZED_HAIRCUT rather than
    # multiplying.
    borrowed = state.get('borrowed_xlm', 0.0)
    if borrowed > 0:
        mark = marks.get(_assets.NATIVE)
        price = _portfolio.mark_price(mark) if mark is not None else None
        if price is None:
            unpriced.append('XLM:short-liability')  # can't mark it -> don't silently ignore a real liability
        else:
            total -= borrowed * price / UNREALIZED_HAIRCUT

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

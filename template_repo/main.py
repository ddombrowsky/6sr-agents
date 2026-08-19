#!/usr/bin/env python3
"""Threshold trading strategy for XLM.

XLM is the only asset this domain trades. Until 2026-08-13 a strategy could also declare
up to two additional Stellar DEX assets in `config.json`'s `assets` array and trade them
through a `decide_asset()` hook; that whole channel was removed, along with the array, the
hook, and the injection/verification machinery behind it. Do not reintroduce a second leg
by reaching around `execute_trade` -- it refuses an undeclared asset, and the revision
smoke test fails any candidate whose state.json comes back holding one.

## Structure matters here -- read before editing

The module top level must contain ONLY imports, assignments, function/class
definitions, the docstring, and the `if __name__ == '__main__'` guard. That is exactly
what `/opt/tools/backtest.py::_is_importable` permits, and if this file steps outside
it, `backtest_strategy()` silently stops importing `decide` and falls back to replaying
the plain config thresholds instead -- so a revision's real logic is invisible to its own
fitness check, while the backtest still returns confident-looking numbers.

Two consequences that are easy to trip over:

  * `sys.path = sys.path + ['/opt/tools']` is written as a plain assignment on purpose.
    The obvious `sys.path.append('/opt/tools')` is a bare call expression, which is NOT
    on the whitelist -- that one line alone is enough to make this file un-importable.
    Do not "tidy" it back.
  * The trading loop lives in `main()` under the `__main__` guard, not at top level, so
    importing this module for a backtest never starts trading.

## Where to put strategy logic

`decide()` is the whole of it, and is what `backtest_strategy` replays. It returns
`(side, action, requested_usd)` or None. Return a 3-tuple, not a 4-tuple: backtest's
`_normalize` accepts anything of length >= 3 and would silently drop a fourth element.

Execution -- balance mutation, overdraft/oversell clamping, trade logging, and live
submission -- belongs to `trade_logger.execute_trade`. Do not reimplement any of it here.

## Non-price signals go through `state`, never through a call inside decide()

`decide()` must stay pure and fast: backtest.py calls it once per tick, ~86,000 times
for a 30-day replay. A network call in there (news, an oracle, an API) would fire
86,000 requests and turn a one-second fitness check into hours -- and would answer
every historical tick with *today's* data, which is not a backtest of anything.

So the loop in `main()` fetches such signals once per tick and puts them in `state`;
`decide()` reads them with a neutral default. `state['news_sentiment']` is the worked
example. In a backtest the key is simply absent, `.get(..., 0.0)` yields neutral, and
the rest of the rule replays unchanged.

Which is also the honest limitation: there is no historical headline archive here, so
a news component is invisible to `beats_buy_hold` -- the backtest always judges it at
neutral. What *does* see it is score.py, which ranks real paper net worth. A news edge
has to prove itself in live paper trading over hours, not in the backtest.

## The DEX/CEX basis: the same pattern, with one difference that matters

`state['basis_bp']` and `state['basis_tradeable_bp']` arrive exactly like sentiment does
-- fetched once per tick by `main()`, read from `state` with a neutral default. They are
the gap between the CEX aggregate this strategy *decides* on (`price_feed.get_price()`)
and the Stellar DEX book it *executes* against. Positive means the DEX is rich (a good
place to sell, a bad place to buy); `basis_tradeable_bp` is that gap minus the cost of
crossing to capture it, and is usually negative -- which is itself the signal, because it
says when NOT to trade.

Fetch it with `basis.latest()`, which reads a recorded series, and NEVER with
`basis.get_basis()` / `dex_is_cheap()` / `dex_is_rich()`, which each do live Horizon
calls. Those belong to tooling, not to a tick loop -- see basis.py's docstring.

The difference from news: this one IS partially replayable. `market_recorder` keeps a
per-minute history, and `backtest.py` joins it to the candle grid, so a basis rule does
reach its own fitness check -- but only on a 1-minute replay (`interval=1, days=0.5`)
and only as far back as the recording goes. Check `basis_coverage` in the result before
believing any of it, and read `basis_edge_excess_bp` / `beats_basis_null` rather than
`beats_buy_hold` on such a short window.
"""
import json
import sys
import time
from pathlib import Path

# Plain assignment, deliberately. See the module docstring.
sys.path = sys.path + ['/opt/tools']

import basis
import news_feed
import portfolio
from price_feed import get_price
from trade_logger import execute_trade

CONFIG_PATH = Path('config.json')
STATE_PATH = Path('state.json')
TICK_SECONDS = 30
MAX_HISTORY = 500

# Shorting (SHORTING_PLAN.md, XLM leg only): forces a cover once the buy-back would cost
# this multiple of what the short actually received. 1.5 = a 50% adverse move forces a
# cover -- deliberately in the tick loop, not in decide(), because it has to fire on
# ticks where decide() returns nothing; a price moving hard against an open short can't
# wait for the next vote.
SHORT_STOP_OUT_RATIO = 1.5


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return portfolio.normalize_state(json.load(f))
        except Exception as e:
            print(f'could not read state.json ({e}); starting fresh')
    return portfolio.normalize_state({'balance_usd': 1000.0, 'balance_xlm': 0.0})


def save_state(state):
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f)


def current_sentiment(asset='XLM'):
    """This tick's news sentiment in [-1, 1], or 0.0 (neutral) if unavailable.

    Called once per tick from main(), never from decide(). news_feed caches for
    ~15 minutes, so the per-tick cost is a dict lookup and the real fetch happens
    a couple of times an hour. Never raises: a news outage must not kill a trading
    loop that strat_manager only ever starts once.
    """
    try:
        return float(news_feed.sentiment_score(asset=asset))
    except Exception as e:
        print(f'news sentiment unavailable ({e}); treating as neutral')
        return 0.0


def current_basis(spec='XLM'):
    """This tick's recorded DEX/CEX basis dict, or None if there isn't a usable one.

    Called once per tick from main(), never from decide(). basis.latest() reads the
    series market_recorder's daemon writes once a minute, so this is a small file read
    rather than a Horizon call -- which is the entire reason it is latest() and not
    get_basis(): every running strategy calling get_basis() on a 30s tick would be tens
    of order-book requests a minute against a book nobody caches.

    Never raises. main() is started once by strat_manager and never restarted, so an
    exception here would not degrade a strategy, it would end it.
    """
    try:
        return basis.latest(spec)
    except Exception as e:
        print(f'basis unavailable ({e}); treating as unknown')
        return None


def basis_ok(side, state, config):
    """Does the venue currently justify this trade? True whenever it can't be answered.

    Fails NEUTRAL, and the distinction from basis.dex_is_cheap() is the point: that
    function answers "is there an edge here" and correctly says False when it cannot
    tell. This is a veto on a trade the price rule already wants, so unknown must mean
    "don't block". A veto that failed closed would silently halt all buying the moment
    the recorder died, and since score.py ranks realized net worth, that would look like
    a strategy choice rather than an outage.

    Off unless config.json carries `basis_min_bp`, so a strategy without the knob
    behaves byte-for-byte as it did before this existed.
    """
    threshold = config.get('basis_min_bp')
    if threshold is None:
        return True

    tradeable = state.get('basis_tradeable_bp')
    basis_bp = state.get('basis_bp')
    if tradeable is None or basis_bp is None:
        return True                       # unknown -> neutral, never a block

    if tradeable < threshold:
        return False                      # the dislocation isn't worth the toll
    # Sign check: a dislocation only helps the side it points at. Negative basis means
    # the DEX is cheap against the CEX, which is good to buy into and bad to sell into.
    return basis_bp < 0 if side == 'buy' else basis_bp > 0


def decide(price, history, state, config):
    """XLM leg. Returns (side, action, requested_usd) or None.

    This exact signature is what backtest.py replays and what `beats_buy_hold` is
    measured on, so keep it even if the body changes completely.

    Sentiment is used as a veto on buys, not as a trigger: it only ever skips a trade
    the price rule already wanted. That is the cheap direction -- a skipped marginal
    buy keeps the round-trip friction it would have paid -- and it degrades to the
    plain threshold rule when the feed is down or the key is missing.

    Sells are deliberately never vetoed. Blocking an exit on a news reading can strand
    a position indefinitely, which is a far worse failure than a missed entry.
    """
    buy_below = config.get('buy_below')
    sell_above = config.get('sell_above')
    size = config.get('trade_amount_usd', 10)

    # Supplied by main()'s tick loop. Absent on every backtest tick -> 0.0 -> the
    # veto below never fires and this is exactly the threshold rule it always was.
    sentiment = state.get('news_sentiment', 0.0)
    veto_below = config.get('news_veto_below', -0.5)

    if buy_below and price <= buy_below:
        if veto_below is not None and sentiment <= veto_below:
            return None
        # Buys only, for the same reason sentiment vetoes buys only: a venue reading
        # that can block an exit can strand a position. A revision is free to extend
        # this to sells -- basis_report.py will say whether that helped.
        if not basis_ok('buy', state, config):
            return None
        return ('buy', 'buy', size)
    if sell_above and price >= sell_above:
        return ('sell', 'sell', size)
    return None


def main():
    config = load_config()
    agent_name = config.get('name', 'unnamed')
    state = load_state()

    print(f"Agent {agent_name} starting with USD {state['balance_usd']:.2f}, "
          f"XLM {state['balance_xlm']:.4f}")

    # Persist once before the first tick. monitor.py's smoke test reverts any revision
    # that hasn't written a readable state.json within SMOKE_TEST_SECONDS, and a first
    # tick can outlast that: a price fetch plus a 30s sleep.
    save_state(state)

    history = []

    while True:
        price = get_price()
        if price is None:
            time.sleep(TICK_SECONDS)
            continue

        history.append(price)
        del history[:-MAX_HISTORY]

        # Fetched here, once per tick, and handed to decide() through state -- never
        # fetched inside decide() itself. See the module docstring for why.
        state['news_sentiment'] = current_sentiment()

        # Same contract, and note the pop. state is persisted to state.json and reloaded
        # on restart, so leaving the last known basis in place when the recorder goes
        # quiet would let a strategy gate on a reading from before it was restarted --
        # stale but indistinguishable from fresh. Absent means unknown, and basis_ok
        # treats unknown as neutral.
        b = current_basis()
        if b:
            state['basis_bp'] = b['basis_bp']
            state['basis_tradeable_bp'] = b['tradeable_bp']
        else:
            state.pop('basis_bp', None)
            state.pop('basis_tradeable_bp', None)

        # Stop-out check: must run every tick, independent of decide(), because a price
        # moving hard against an open short can't wait for the next vote. See
        # SHORT_STOP_OUT_RATIO above.
        borrowed = state.get('borrowed_xlm', 0.0)
        if borrowed > 0:
            proceeds = state.get('short_proceeds_usd', 0.0)
            buyback_cost = borrowed * price
            if buyback_cost > proceeds * SHORT_STOP_OUT_RATIO:
                state = execute_trade(agent_name, 'cover_stoploss', 'buy', price, buyback_cost,
                                      state, allow_shorting=True)

        decision = decide(price, history, state, config)
        if decision:
            side, action, requested_usd = decision
            state = execute_trade(agent_name, action, side, price, requested_usd, state,
                                  allow_shorting=config.get('allow_shorting', False))

        save_state(state)
        time.sleep(TICK_SECONDS)


if __name__ == '__main__':
    main()

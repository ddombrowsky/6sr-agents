#!/usr/bin/env python3
"""Threshold trading strategy: XLM plus up to two additional Stellar assets.

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

`decide()` is the XLM leg and is what `backtest_strategy` replays; `decide_asset()` is
called once per extra asset per tick. Both return `(side, action, requested_usd)` or
None. Return a 3-tuple, not a 4-tuple: backtest's `_normalize` accepts anything of
length >= 3 and would silently drop a fourth element.

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
"""
import json
import sys
import time
from pathlib import Path

# Plain assignment, deliberately. See the module docstring.
sys.path = sys.path + ['/opt/tools']

import news_feed
import portfolio
from dex_price import get_mark
from price_feed import get_price
from trade_logger import execute_trade

CONFIG_PATH = Path('config.json')
STATE_PATH = Path('state.json')
TICK_SECONDS = 30
MAX_HISTORY = 500


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
        return ('buy', 'buy', size)
    if sell_above and price >= sell_above:
        return ('sell', 'sell', size)
    return None


def decide_asset(asset, price, history, state, config):
    """One extra (non-XLM) leg. Returns (side, action, requested_usd) or None.

    `asset` is a dict from portfolio.assets_from_config: code, issuer, spec, and that
    leg's own buy_below / sell_above / trade_amount_usd. `history` is that asset's own
    price history, not XLM's.

    The default is the same threshold rule applied per leg, so a strategy gets working
    multi-asset behavior from config.json alone. Extra assets are DEX-only: thinner
    books, wider spreads, and far less history than XLM -- size them smaller.
    """
    buy_below = asset.get('buy_below')
    sell_above = asset.get('sell_above')
    size = asset.get('trade_amount_usd', config.get('trade_amount_usd', 10))

    if buy_below and price <= buy_below:
        return ('buy', 'buy', size)
    if sell_above and price >= sell_above:
        return ('sell', 'sell', size)
    return None


def main():
    config = load_config()
    agent_name = config.get('name', 'unnamed')
    state = load_state()
    extra_assets = portfolio.assets_from_config(config)

    print(f"Agent {agent_name} starting with USD {state['balance_usd']:.2f}, "
          f"XLM {state['balance_xlm']:.4f}"
          + (f", extra assets: {[a['code'] for a in extra_assets]}" if extra_assets else ''))

    # Persist once before the first tick. monitor.py's smoke test reverts any revision
    # that hasn't written a readable state.json within SMOKE_TEST_SECONDS, and a first
    # tick can outlast that: a price fetch per leg plus a 30s sleep.
    save_state(state)

    history = []
    leg_history = {a['spec']: [] for a in extra_assets}

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

        decision = decide(price, history, state, config)
        if decision:
            side, action, requested_usd = decision
            state = execute_trade(agent_name, action, side, price, requested_usd, state)

        for asset in extra_assets:
            leg_price = get_mark(asset['spec'])
            if leg_price is None or leg_price <= 0:
                continue      # never trade a leg we cannot price
            hist = leg_history[asset['spec']]
            hist.append(leg_price)
            del hist[:-MAX_HISTORY]

            decision = decide_asset(asset, leg_price, hist, state, config)
            if decision:
                side, action, requested_usd = decision
                state = execute_trade(agent_name, action, side, leg_price, requested_usd,
                                      state, asset=asset['spec'])

        save_state(state)
        time.sleep(TICK_SECONDS)


if __name__ == '__main__':
    main()

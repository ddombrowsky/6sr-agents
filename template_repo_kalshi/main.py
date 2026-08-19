#!/usr/bin/env python3
"""Kalshi-prediction-market template strategy: read a real market's price, answer p_hat.

## Structure matters here -- read before editing

Same rule as template_repo_forecast/main.py: the module top level must contain ONLY
imports, assignments, function/class definitions, the docstring, and the `if __name__ ==
'__main__'` guard. That is exactly what /opt/tools/kalshi_backtest.py::importability_report
permits, and if this file steps outside it, replay() silently stops importing decide()
and falls back to the mechanical confidence_gain-only rule instead -- so a revision's
real logic becomes invisible to its own fitness check, while replay() still returns
confident-looking numbers for the fallback.

`sys.path = sys.path + ['/opt/tools']` is written as a plain assignment on purpose, same
reason as template_repo_forecast/main.py: `sys.path.append(...)` is a bare call
expression, which is NOT on the whitelist. Do not "tidy" it back.

The tick loop lives in `main()` under the `__main__` guard, not at top level, so
importing this module for a replay never starts it.

## Where strategy logic goes

`decide(market, history, state, config)` is what kalshi_backtest.py replays. It receives
ONE currently-open Kalshi market at a time (kalshi_recorder.py's normalized shape:
`implied_prob`, `volume`, `open_interest`, `close_time`, ...) and must return a
probability `p_hat` in [0, 1] -- the model's calibrated belief that this market resolves
Yes. It must stay pure and fast: kalshi_backtest.py replays it against thousands of fixed
historical readings per run, and it never sees the eventual outcome or even whether the
market has closed yet -- there would be nothing left to forecast if it did.

The default rule here is NOT "copy the market price": that is mathematically the
UNBEATABLE optimum of any metric built only from (p_hat, p_market) with no outcome (see
master_agent/domain_kalshi.py's docstring), so a strategy that starts there has nowhere
to go but down. Instead the default reads `history` -- this strategy's own past
`implied_prob` readings for THIS ticker, oldest first, capped at HISTORY_LEN -- and
follows the market's recent drift, scaled by `confidence_gain`: a real, if naive,
hypothesis (momentum) that is distinct from the market's current snapshot and gives a
mutation or revision something concrete to test and improve. `confidence_gain=0` reduces
this to the honest zero-edge baseline (pure copy); revising it into something that reads
volume, spread (yes_ask - yes_bid), or an orthogonal signal (e.g. tools/news_feed.py) is
exactly the kind of change this template exists to invite.

## Where scoring happens -- nowhere in this file

/opt/tools/kalshi_recorder.py's `submit_forecast(name, ticker, p_hat)` is the only judge
of record: it freezes the market's current price alongside p_hat, and the real Brier
score -- against the eventual outcome, once kalshi_reconcile.py finds one -- is computed
later from that pair, never from anything this file claims about itself. `main()` below
keeps a running `forecasts_made` tally in state.json purely so
DOMAIN.check_smoke_state's proof-of-life check has something cheap to read; it is not a
score (see kalshi_recorder.py's cumulative_brier()).
"""
import json
import sys
import time
from pathlib import Path

# Plain assignment, deliberately. See the module docstring.
sys.path = sys.path + ['/opt/tools']

import kalshi_recorder

CONFIG_PATH = Path('config.json')
STATE_PATH = Path('state.json')
TICK_SECONDS = 60          # kalshi_recorder polls every 300s; no point ticking faster
HISTORY_LEN = 20            # readings kept per ticker, for the momentum default


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception as e:
            print(f'could not read state.json ({e}); starting fresh')
    return {'forecasts_made': 0, 'history': {}}


def save_state(state):
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f)


def decide(market, history, state, config):
    """Return a probability p_hat in [0, 1] that `market` resolves Yes.

    `market` is one entry from kalshi_recorder.current_markets()'s shape. `history` is
    this strategy's own past `implied_prob` readings for market['ticker'], oldest first
    -- not past outcomes, which are never visible here (same shape as
    template_repo_forecast/main.py's `history` of past features, not past scores).
    """
    p_market = market.get('implied_prob')
    if p_market is None:
        return 0.5
    gain = config.get('confidence_gain', 1.0)
    drift = (history[-1] - history[0]) if len(history) >= 2 else 0.0
    return min(1.0, max(0.0, p_market + gain * drift))


def main():
    config = load_config()
    agent_name = config.get('name', 'unnamed')
    state = load_state()
    state.setdefault('history', {})
    print(f"Agent {agent_name} starting, {state.get('forecasts_made', 0)} forecast(s) so far")

    # Persist once before the first tick. monitor.py's smoke test reverts any revision
    # that hasn't written a readable state.json within SMOKE_TEST_SECONDS, and a first
    # tick can outlast that on a slow interpreter start.
    save_state(state)

    while True:
        n = int(config.get('markets_per_tick', 1))
        category = config.get('category_filter')
        markets = kalshi_recorder.current_markets(n=n, category=category)
        for market in markets:
            ticker = market.get('ticker')
            if not ticker:
                continue
            ticker_history = state['history'].get(ticker, [])
            try:
                p_hat = decide(market, ticker_history, state, config)
            except Exception as e:
                print(f'decide() raised {type(e).__name__}: {e}; skipping {ticker}')
                continue
            ticker_history = ticker_history + [market.get('implied_prob')]
            state['history'][ticker] = ticker_history[-HISTORY_LEN:]
            entry = kalshi_recorder.submit_forecast(agent_name, ticker, p_hat)
            if entry is None:
                print(f'forecast for {ticker} was rejected (bad p_hat or unpriced market)')
                continue
            state['forecasts_made'] = state.get('forecasts_made', 0) + 1
        save_state(state)
        time.sleep(TICK_SECONDS)


if __name__ == '__main__':
    main()

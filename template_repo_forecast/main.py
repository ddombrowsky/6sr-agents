#!/usr/bin/env python3
"""Forecasting-benchmark template strategy: read a noisy feature, answer a probability.

## Structure matters here -- read before editing

Same rule as template_repo/main.py: the module top level must contain ONLY imports,
assignments, function/class definitions, the docstring, and the `if __name__ == '__main__'`
guard. That is exactly what /opt/tools/forecast_backtest.py::importability_report permits,
and if this file steps outside it, replay() silently stops importing decide() and falls
back to the mechanical confidence_gain-only rule instead -- so a revision's real logic
becomes invisible to its own fitness check, while replay() still returns confident-
looking numbers for the fallback.

`sys.path = sys.path + ['/opt/tools']` is written as a plain assignment on purpose, same
reason as template_repo/main.py: `sys.path.append(...)` is a bare call expression, which
is NOT on the whitelist. Do not "tidy" it back.

The tick loop lives in `main()` under the `__main__` guard, not at top level, so
importing this module for a replay never starts it.

## Where strategy logic goes

`decide(feature, history, state, config)` is what forecast_backtest.py replays. It
receives ONE question's visible feature at a time and must return a probability `p_hat`
in [0, 1] -- the model's calibrated belief that this question resolves True. It must stay
pure and fast: forecast_backtest.py calls it thousands of times per replay, and it never
sees the true outcome or even the question's id -- there would be nothing left to
forecast if it did. See /opt/tools/forecast_engine.py's docstring for the exact
generative model behind `feature`.

The default rule is linear calibration: `p_hat = 0.5 + confidence_gain * (feature -
0.5)`, clipped to [0, 1]. `confidence_gain` lives in config.json, not here, so mechanical
mutation (DOMAIN.tweak_config) and every later revision can find and adjust it without a
code change. Inventing a genuinely new knob is fine -- read it with
`config.get('your_key', <sensible default>)`, never `config['your_key']`, so a fresh
template spawn that never set it still runs.

## Where scoring happens -- nowhere in this file

/opt/tools/forecast_engine.py's `submit_forecast(name, question_id, p_hat)` is the only
judge: it knows the true outcome (this file never does) and returns the Brier score this
forecast earned. `main()` below keeps a running tally of that in state.json
(`forecasts_made`/`brier_sum`/`base_brier_sum`) purely so DOMAIN.score() -- a fallback
that never sees the forecast log, see master_agent/domain_forecast.py's docstring -- has
something to read. The authoritative score always comes from the log via
DOMAIN.score_path(), not from this tally, so there is nothing to gain by tampering with
it here.
"""
import json
import sys
import time
from pathlib import Path

# Plain assignment, deliberately. See the module docstring.
sys.path = sys.path + ['/opt/tools']

import forecast_engine

CONFIG_PATH = Path('config.json')
STATE_PATH = Path('state.json')
TICK_SECONDS = 30


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
    return {'forecasts_made': 0, 'brier_sum': 0.0, 'base_brier_sum': 0.0}


def save_state(state):
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f)


def decide(feature, history, state, config):
    """Return a probability p_hat in [0, 1] for this one question's visible feature.

    Ignoring `feature` entirely (always returning 0.5) scores exactly the base rate;
    reading it well is the whole game. `history` is this strategy's own past features,
    oldest first -- not past outcomes, which it never learns (same shape as
    template_repo/main.py's `history` of past prices, not past P&L).
    """
    gain = config.get('confidence_gain', 1.0)
    return min(1.0, max(0.0, 0.5 + gain * (feature - 0.5)))


def main():
    config = load_config()
    agent_name = config.get('name', 'unnamed')
    state = load_state()
    print(f"Agent {agent_name} starting, {state.get('forecasts_made', 0)} forecast(s) so far")

    # Persist once before the first tick. monitor.py's smoke test reverts any revision
    # that hasn't written a readable state.json within SMOKE_TEST_SECONDS, and a first
    # tick can outlast that on a slow interpreter start.
    save_state(state)

    history = []
    last_tick = None

    while True:
        tick = forecast_engine.current_tick()
        if tick != last_tick:
            last_tick = tick
            n = int(config.get('questions_per_tick', 1))
            for q in forecast_engine.current_questions(n):
                try:
                    p_hat = decide(q['feature'], history, state, config)
                except Exception as e:
                    print(f'decide() raised {type(e).__name__}: {e}; skipping this question')
                    continue
                history.append(q['feature'])
                del history[:-500]
                entry = forecast_engine.submit_forecast(agent_name, q['id'], p_hat)
                if entry is None:
                    print(f'forecast for {q["id"]} was rejected (bad p_hat or id)')
                    continue
                state['forecasts_made'] = state.get('forecasts_made', 0) + 1
                state['brier_sum'] = state.get('brier_sum', 0.0) + entry['brier']
                state['base_brier_sum'] = state.get('base_brier_sum', 0.0) + entry['base_brier']
            save_state(state)
        time.sleep(TICK_SECONDS)


if __name__ == '__main__':
    main()

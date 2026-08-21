#!/usr/bin/env python3
"""Null-domain template strategy: answer coin-flip questions, bank points.

This is the seed genome for `DOMAIN=null`, the reference domain that exists to prove
monitor.py's loop carries a game with no prices, no assets, no order book and no money
(see master_agent/domain_null.py's docstring). Everything here is deliberately
self-contained: nothing under /opt/tools is imported, because having none of that
furniture is the entire point of the domain.

## Structure matters here -- read before editing

Same rule as template_repo/main.py and template_repo_forecast/main.py: the module top
level holds ONLY imports, assignments, function/class definitions, the docstring and the
`if __name__ == '__main__'` guard. The tick loop lives in `main()` under the guard, so
importing this module never starts it. domain_null.importability() returns None (no
replay engine imports the genome here), so nothing mechanically enforces this -- keep to
it anyway, because the next domain to be built from this skeleton will have a replay
engine that does.

## The game

Each tick the strategy draws `questions_per_tick` questions. A question shows exactly one
number, `feature`, in [0, 1]; it is the true probability that the question resolves True.
`decide()` sees only that and returns True, False, or None to skip.

The outcome is drawn in `main()` AFTER decide() has returned, from a number decide() was
never handed -- so there is nothing to read ahead to, and a revision that tries will find
no outcome anywhere in the question dict. Answering correctly pays +1, wrong pays -1, and
every answer -- right or wrong -- is charged ANSWER_COST. Skipping pays and costs nothing.

That cost is what makes this a game rather than a counter. Without it, answering is always
non-negative and the only strategy is to answer everything; with it, an answer is only
worth making when the question is lopsided enough to cover it. The default rule below
answers when `|feature - 0.5| >= confidence - 0.5`, which makes `confidence` a
selectivity threshold: at 0.5 it answers everything, at 1.0 it answers nothing. The
steady-state payoff of that rule is `(2 - 2c) * (c - ANSWER_COST)` per question, which
peaks at c = 0.625 -- an interior optimum, inside the range DOMAIN.seed_config seeds from
and reachable by DOMAIN.tweak_config's +/-5% jitter. Finding it is the whole exercise.

`confidence` and `questions_per_tick` live in config.json, not here, so mechanical
mutation can find them without a code change. Read any knob you invent with
`config.get('your_key', <default>)`, never `config['your_key']`, so a fresh template
spawn that never set it still runs.

## What gets scored

domain_null.score() reads `points` out of state.json and adds it to STARTING_SCORE. That
number is self-reported and nothing audits it -- domain_null's docstring says so plainly,
and it is the one thing that makes this domain a skeleton rather than a real benchmark
(domain_forecast.py is the real one, judged by a module the strategy does not own).

`points` is the total banked over the last POINTS_WINDOW_S seconds, not since birth. A
lifetime total would make score a function of age -- every strategy's score climbing
forever, the oldest always on top, and rank-culling measuring birthday rather than skill.
A fixed time window is also what stops over-selectivity from winning: a per-answer average
would reward answering almost nothing, whereas a tick spent skipping banks nothing into a
window that is emptying regardless.
"""
import json
import random
import time
from pathlib import Path

CONFIG_PATH = Path('config.json')
STATE_PATH = Path('state.json')

# monitor.py reads this log through domain_null.activity() for its idle detection, one
# JSON object per line with a `timestamp`. Same shape sdex's trade log uses.
TRADES_DIR = Path('/opt/trades')

TICK_SECONDS = 5
ANSWER_COST = 0.25
POINTS_WINDOW_S = 600
MAX_HISTORY = 500


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
    return {'points': 0.0, 'answered': 0, 'correct': 0, 'skipped': 0, 'recent': []}


def save_state(state):
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f)


def log_answer(agent_name, entry):
    """Append one resolved answer to the activity log. Never fatal: a strategy that
    cannot write its log is still playing, and dying here would look to monitor like a
    main.py that exits on its own."""
    try:
        TRADES_DIR.mkdir(parents=True, exist_ok=True)
        with (TRADES_DIR / f'{agent_name}.log').open('a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        print(f'could not write activity log ({e}); continuing')


def new_question():
    """One question, showing only its `feature`. The outcome is not drawn here."""
    return {'id': f'q_{time.time():.3f}_{random.randrange(1 << 24):06x}',
            'feature': random.random()}


def bank(state, delta, now):
    """Drop anything that has aged out of the window, add `delta`, recompute `points`.

    A zero `delta` prunes and recomputes without recording anything, which is what the
    end-of-tick call wants: an answer is never worth exactly zero (it is +1 or -1 less
    ANSWER_COST), so a zero entry would only be a placeholder crowding out real ones.
    """
    recent = [pair for pair in state.get('recent', []) if now - pair[0] < POINTS_WINDOW_S]
    if delta:
        recent.append([now, delta])
    del recent[:-MAX_HISTORY]
    state['recent'] = recent
    state['points'] = round(sum(pair[1] for pair in recent), 4)


def decide(question, history, state, config):
    """Return True, False, or None to skip this question.

    `history` is this strategy's own past features, oldest first -- not past outcomes,
    which it never learns. Pure and fast: it is called once per question and nothing
    else in this file depends on how long it takes.
    """
    confidence = float(config.get('confidence', 0.6))
    feature = question['feature']
    if abs(feature - 0.5) < confidence - 0.5:
        return None
    return feature >= 0.5


def main():
    config = load_config()
    agent_name = config.get('name', 'unnamed')
    state = load_state()
    print(f"Agent {agent_name} starting with {state.get('points', 0.0):+.2f} points "
          f"({state.get('answered', 0)} answered)")

    # Persist once before the first tick. monitor.py's smoke test reverts any revision
    # that has not written a readable state.json within SMOKE_TEST_SECONDS, and a first
    # tick can outlast that on a slow interpreter start.
    save_state(state)

    history = []

    while True:
        now = time.time()
        for _ in range(max(1, int(config.get('questions_per_tick', 1)))):
            question = new_question()
            try:
                guess = decide(question, history, state, config)
            except Exception as e:
                print(f'decide() raised {type(e).__name__}: {e}; skipping this question')
                continue
            history.append(question['feature'])
            del history[:-MAX_HISTORY]

            if guess is None:
                state['skipped'] = state.get('skipped', 0) + 1
                continue

            # Drawn only now, from a number decide() never saw. See the docstring.
            outcome = random.random() < question['feature']
            correct = bool(guess) == outcome
            delta = (1.0 if correct else -1.0) - ANSWER_COST

            state['answered'] = state.get('answered', 0) + 1
            state['correct'] = state.get('correct', 0) + (1 if correct else 0)
            bank(state, delta, now)
            log_answer(agent_name, {'timestamp': now, 'name': agent_name,
                                    'question': question['id'],
                                    'feature': round(question['feature'], 4),
                                    'guess': bool(guess), 'outcome': outcome,
                                    'points': round(delta, 4)})

        # Keep `points` honest on a tick that answered nothing: the window is emptying
        # whether or not anything was banked into it, and a strategy that has gone quiet
        # should show that in its score rather than holding its last good number.
        bank(state, 0.0, now)
        save_state(state)
        time.sleep(TICK_SECONDS)


if __name__ == '__main__':
    main()

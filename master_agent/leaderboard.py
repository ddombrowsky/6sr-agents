#!/usr/bin/env python3
"""Print every registered strategy with its status and the SAME score monitor.py ranks
it on -- generic across domains, not specific to any one of them.

This used to read balance_usd/balance_xlm out of each strategy's state.json and price
XLM itself via tools/price_feed.py -- fine for sdex, meaningless for a domain with no
price and no balances (e.g. domain_forecast.py). Now it asks the active DOMAIN (see
domain.py) for everything domain-specific: the per-cycle observation (DOMAIN.observe()),
each strategy's score (DOMAIN.score_path, called exactly the way monitor.py's
compute_strategy_score does), its activity count (DOMAIN.activity), and whether its
main.py is visible to DOMAIN's replay (DOMAIN.importability).

Imports monitor.py directly, not just domain.py, and reuses its compute_strategy_score/
is_idle/IDLE_GRACE_S rather than re-deriving them -- so the rank order shown here can
never drift from what the evolutionary loop actually ranks on. This mirrors
selftest_domain.py's own `import monitor` pattern (see its section 5).
"""
import json
import sys
from pathlib import Path

STATE_FILE = Path('/opt/strategy_state.json')

# monitor.py/domain.py live next to this file; __file__ may be a symlink (/opt/
# leaderboard.py -> master_agent/leaderboard.py), so resolve it rather than trust
# sys.path[0].
sys.path.append(str(Path(__file__).resolve().parent))
import monitor

DOMAIN = monitor.DOMAIN


def load_state():
    if STATE_FILE.exists():
        return json.load(STATE_FILE.open())
    return {}


def borrowed_xlm_for(info):
    """XLM currently owed against the short facility (SHORTING_PLAN.md), or None.

    Read straight from the strategy's own state.json rather than through DOMAIN: unlike
    score/activity/importability, which differ by domain, `borrowed_xlm` is a portfolio.py
    field with no domain-specific meaning to abstract -- shorting is XLM-only and sdex-only,
    so a strategy under a domain that never writes this key (or has no state.json at all)
    simply reports None, the same "not applicable here" this file already shows as N/A for
    score. Fails open: a strategy with an unreadable state.json is not this column's problem
    to raise, it's read-only reporting on top of state monitor.py already trusts.
    """
    state_path = Path(info.get('path', '')) / 'state.json'
    try:
        raw = json.load(state_path.open())
        return float(raw.get('borrowed_xlm') or 0.0)
    except Exception:
        return None


def replay_blind(state):
    """Strategies whose main.py DOMAIN.importability() reports as not replayable.

    Fails open per-strategy -- a tooling hiccup on one strategy must not hide the whole
    footnote, and this is read-only reporting, not a gate the way DOMAIN.replay()'s
    caller in monitor.py is.
    """
    blind = []
    for name, info in state.items():
        main_py = Path(info.get('path', '')) / 'main.py'
        if not main_py.exists():
            continue
        try:
            verdict = DOMAIN.importability(str(main_py))
        except Exception:
            continue
        if verdict is not None and not verdict[0]:
            blind.append(name)
    return blind


def main():
    state = load_state()
    if not state:
        print('No strategies registered.')
        return

    obs = DOMAIN.observe()
    if obs is None:
        print(f'{DOMAIN.OBSERVE_FAILURE_NOTE}; scores will be shown as N/A this run.')
    else:
        obs = DOMAIN.observe_population(obs, state)

    rows = []
    idle_names = set()
    for name, info in state.items():
        count, first_ts, last_ts = DOMAIN.activity(name)
        if monitor.is_idle(name, count, info):
            idle_names.add(name)
        if obs is None:
            score = None
        else:
            score = monitor.compute_strategy_score(name, info, obs)
            if score == -float('inf'):
                score = None       # display-only; the raw -inf still sorts last below
        age_s = monitor.strategy_age_s(info)
        max_score = info.get('max_score')
        borrowed = borrowed_xlm_for(info)
        rows.append((name, info.get('status'), info.get('pid'), score, count, age_s,
                    max_score, borrowed))

    # The same order monitor.run() ranks on: strategies that have ever acted first (see
    # monitor.IDLE_GRACE_S), then by score, then by action count as a tiebreak.
    rows.sort(key=lambda r: (r[0] not in idle_names,
                             r[3] if r[3] is not None else -float('inf'), r[4]),
              reverse=True)

    name_w = max(len(r[0]) for r in rows) + 2
    header = (f"{'NAME':<{name_w}}{'STATUS':<10}{'PID':<8}{'SCORE':>16}{'MAX SCORE':>16}"
              f"{'BORROWED':>12}{'ACTIONS':>10}{'AGE(h)':>9}  {'IDLE'}")
    print(header)
    print('-' * len(header))
    for name, status, pid, score, count, age_s, max_score, borrowed in rows[:20]:
        score_s = f'{score:.2f}' if score is not None else 'N/A'
        max_score_s = f'{max_score:.2f}' if max_score is not None else 'N/A'
        # 0.00 (a real, checked zero) is left distinct from N/A (couldn't say, or this
        # domain has no such thing) -- collapsing them would hide a state.json read
        # failure behind what looks like a clean, debt-free strategy.
        borrowed_s = f'{borrowed:.2f}' if borrowed is not None else 'N/A'
        pid_s = str(pid) if pid else '-'
        age_s_str = f'{age_s / 3600:.1f}' if age_s is not None else 'N/A'
        idle_s = 'yes' if name in idle_names else ''
        print(f'{name:<{name_w}}{status or "unknown":<10}{pid_s:<8}{score_s:>16}{max_score_s:>16}'
              f'{borrowed_s:>12}{count:>10}{age_s_str:>9}  {idle_s}')

    print(f'\nDomain: {DOMAIN.NAME}')
    if obs is not None:
        print(DOMAIN.observation_line(obs).rstrip())

    blind = replay_blind(state)
    if blind:
        # An aggregate, not a per-row column: these rank normally, DOMAIN.replay() just
        # can't see their real logic and falls back to whatever it does for a domain-
        # thresholds-only replay -- worth knowing in aggregate, not worth a column.
        print(f'NOTE: {len(blind)} of {len(state)} strategies are replay-blind '
              f'(DOMAIN.importability() says main.py is not importable as written): '
              f'{", ".join(sorted(blind))}')


if __name__ == '__main__':
    main()

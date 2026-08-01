#!/usr/bin/env python3
"""Monitor script for XLM paper trading strategies.
Runs an infinite loop checking strategy performance every hour.
Every cycle it ranks *all* known strategies (running or stopped) by score,
stops anything ranked below KEEP_TOP_N, clones the top two with slightly
tweaked/revised thresholds, and makes sure the new clones plus the rest of the
top N are running.
"""
import json
import os
import subprocess
import sys
import time
import datetime
import random
import uuid
from pathlib import Path

from score import score_from_strategy_path

STATE_FILE = Path('/opt/strategy_state.json')
STRATEGIES_DIR = Path('/opt/strategies')
TEMPLATE_REPO = 'file:///opt/template_repo'
MASTER_AGENT_SCRIPT = Path('/opt/master_agent/master-agent.py')
REVISION_TIMEOUT = 6000 # seconds allotted for the LLM to revise a clone before falling back
KEEP_TOP_N = 8 # strategies ranked below this by net worth get stopped each cycle
LIVE_STRATEGY_FILE = Path('/opt/live_strategy.json') # which single strategy trades real money (pubnet-plan.md)

def load_state():
    if STATE_FILE.exists():
        return json.load(STATE_FILE.open())
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def get_current_price():
    # Use the shared price_feed tool
    import sys as _sys
    _sys.path.append('/opt/tools')
    from price_feed import get_price
    return get_price()

def compute_strategy_score(strategy_name, state_entry, price):
    score = score_from_strategy_path(state_entry['path'], price)
    if score is None:
        print(f'Error reading state for {strategy_name}')
        return -float('inf')
    return score

def apply_seed_thresholds(cfg_path, name, price):
    # Used only for bootstrapping: the template's config.json ships with
    # buy_below=sell_above=0.0, which never trades and can't be nudged by
    # apply_random_tweak (0.0 * anything is still 0.0). Seed a real range
    # around the current price instead.
    trade_amount_usd = 10.0
    if cfg_path.exists():
        try:
            trade_amount_usd = json.load(open(cfg_path)).get('trade_amount_usd', 10.0)
        except Exception:
            pass
    new_cfg_data = {
        'name': name,
        'buy_below': round(price * 0.98, 6),
        'sell_above': round(price * 1.02, 6),
        'trade_amount_usd': trade_amount_usd,
    }
    json.dump(new_cfg_data, open(cfg_path, 'w'), indent=2)

def bootstrap_initial_strategies(price, count=2):
    # First run: strategy_state.json doesn't exist yet, so there's nothing to
    # stop or clone-from in the normal cycle below. Seed `count` strategies
    # straight from the template so there's something for later cycles to evolve.
    print(f'No strategies registered yet; bootstrapping {count} initial strategies from the template.')
    for i in range(count):
        name = f'seed_{i}_{int(time.time())}'
        print(f'Bootstrapping strategy {name}')
        subprocess.run(['/opt/strat_manager.py', 'clone', name, TEMPLATE_REPO])
        cfg_path = STRATEGIES_DIR / name / 'config.json'

        revised = False
        if MASTER_AGENT_SCRIPT.exists():
            try:
                result = subprocess.run(
                    ['python3', str(MASTER_AGENT_SCRIPT), 'revise-strategy',
                     name, name, '1000.0', '{}', str(price)],
                    capture_output=True, text=True, timeout=REVISION_TIMEOUT,
                )
                if result.returncode == 0:
                    print(f'Master agent revised {name}:\n{result.stdout.strip()}')
                    revised = True
                else:
                    print(f'Master agent revision failed for {name}: {result.stderr.strip()}')
            except Exception as e:
                print(f'Master agent revision errored for {name}: {e}')

        if not revised and cfg_path.exists():
            print(f'Falling back to seeded thresholds for {name}')
            apply_seed_thresholds(cfg_path, name, price)

        subprocess.run(['/opt/strat_manager.py', 'start', name])

def apply_random_tweak(parent_cfg_path, new_cfg_path, new_name):
    # Fallback used when the master agent is unavailable or fails: copy the parent's
    # config with thresholds nudged by a small random factor (e.g. 0.95 - 1.05).
    parent = json.load(open(parent_cfg_path))
    tweak = random.uniform(0.95, 1.05)
    new_cfg_data = {
        'name': new_name,
        'buy_below': round(parent['buy_below'] * tweak, 6),
        'sell_above': round(parent['sell_above'] * tweak, 6),
        'trade_amount_usd': parent.get('trade_amount_usd', 10.0)
    }
    json.dump(new_cfg_data, open(new_cfg_path, 'w'), indent=2)

def load_live_strategy():
    if LIVE_STRATEGY_FILE.exists():
        try:
            return json.load(LIVE_STRATEGY_FILE.open())
        except Exception:
            return None
    return None

def save_live_strategy(name):
    LIVE_STRATEGY_FILE.write_text(json.dumps({'name': name, 'since': time.time()}, indent=2))

def set_live_flag(name, live):
    # Plain marker file main.py polls for, not a config.json key, so it survives a
    # revision rewriting config.json from scratch. monitor.py alone writes/deletes it
    # — a strategy's own code never decides it's live (pubnet-plan.md).
    flag_path = STRATEGIES_DIR / name / 'live.flag'
    if live:
        flag_path.touch()
    else:
        flag_path.unlink(missing_ok=True)

def promote_live_strategy(current_leader):
    """Ensure `current_leader` (this cycle's #1 by net worth) is the one strategy
    marked live. On a leader change, the flip is gated on stellar_trader.wind_down()
    fully liquidating the outgoing strategy's real pubnet position first — if it can't
    finish in one cycle, the old leader simply stays live and this retries next cycle
    (safe: run()'s cull loop exempts the live strategy from KEEP_TOP_N below).
    """
    live = load_live_strategy()
    if live and live.get('name') == current_leader:
        return

    if live and live.get('name'):
        old_name = live['name']
        print(f'Live strategy changing from {old_name} to {current_leader}; winding down {old_name} first')
        import sys as _sys
        _sys.path.append('/opt/tools')
        from stellar_trader import wind_down
        result = wind_down()
        if not result['liquidated']:
            print(f"wind_down did not fully flatten {old_name} this cycle "
                  f"(remaining_xlm={result['remaining_xlm']}, reason={result['reason']}); "
                  f"{old_name} stays live, retrying next cycle")
            return
        print(f"wind_down flattened {old_name} ({result['chunks']} chunk(s))")
        set_live_flag(old_name, False)

    print(f'Promoting {current_leader} to live')
    set_live_flag(current_leader, True)
    save_live_strategy(current_leader)

def run():
    while True:
        print('--- Monitoring cycle', datetime.datetime.now(), '---')
        price = get_current_price()
        if price is None:
            print('Could not fetch price, skipping this cycle')
            time.sleep(3600)
            continue
        state = load_state()
        if not state:
            bootstrap_initial_strategies(price)
            print('Sleeping for 1 hour...')
            time.sleep(3600)
            continue
        performances = []
        for name, info in state.items():
            score = compute_strategy_score(name, info, price)
            performances.append((name, score))
        performances.sort(key=lambda x: x[1], reverse=True)
        print('Strategy performances (score):')
        for name, score in performances:
            print(f'  {name}: {score:.2f}')

        current_leader = performances[0][0]
        promote_live_strategy(current_leader)
        live_state = load_live_strategy()
        live_name = live_state['name'] if live_state else None

        # Stop everything ranked below KEEP_TOP_N (no-op if already
        # stopped). Re-derived from full-population rank every cycle, so a
        # stopped strategy can't get stuck occupying a "cull" slot forever the
        # way a fixed "worst two" window could. The live strategy is exempt even if
        # it falls out of the top N — it must not be killed while holding a real
        # pubnet position (pubnet-plan.md).
        for name, _ in performances[KEEP_TOP_N:]:
            info = state.get(name)
            if info and info.get('status') == 'running':
                if name == live_name:
                    print(f'Skipping cull for {name}: currently the live pubnet strategy')
                    continue
                print(f'Stopping strategy below rank {KEEP_TOP_N}: {name}')
                subprocess.run(['/opt/strat_manager.py', 'stop', name])

        # Clone the top two and hand each clone to the master agent to revise
        top_two = performances[:2]
        leaderboard = json.dumps({n: score for n, score in performances})
        new_clone_names = []
        for name, score in top_two:
            # create a new unique name (not derived from the parent's name, so it
            # doesn't keep growing across generations of clones-of-clones)
            new_name = f"clone_{uuid.uuid4().hex[:12]}"
            print(f'Cloning best strategy {name} as {new_name}')
            subprocess.run(['/opt/strat_manager.py', 'clone', new_name, TEMPLATE_REPO])
            new_clone_names.append(new_name)
            parent_cfg = Path(state[name]['path']) / 'config.json'
            new_cfg = Path(STRATEGIES_DIR) / new_name / 'config.json'

            revised = False
            if MASTER_AGENT_SCRIPT.exists():
                try:
                    result = subprocess.run(
                        ['python3', str(MASTER_AGENT_SCRIPT), 'revise-strategy',
                         new_name, name, str(score), leaderboard, str(price)],
                        capture_output=True, text=True, timeout=REVISION_TIMEOUT,
                    )
                    if result.returncode == 0:
                        print(f'Master agent revised {new_name}:\n{result.stdout.strip()}')
                        revised = True
                    else:
                        print(f'Master agent revision failed for {new_name}: {result.stderr.strip()}')
                except Exception as e:
                    print(f'Master agent revision errored for {new_name}: {e}')

            if not revised and parent_cfg.exists() and new_cfg.exists():
                print(f'Falling back to random tweak for {new_name}')
                apply_random_tweak(parent_cfg, new_cfg, new_name)

        # Start the new clones, plus any top-N strategy that's currently stopped
        # (reload state first since strat_manager.py clone/stop wrote to disk)
        for new_name in new_clone_names:
            subprocess.run(['/opt/strat_manager.py', 'start', new_name])

        fresh_state = load_state()
        for name, _ in performances[:KEEP_TOP_N]:
            info = fresh_state.get(name)
            if info and info.get('status') == 'stopped':
                print(f'Restarting top-{KEEP_TOP_N} strategy that was stopped: {name}')
                subprocess.run(['/opt/strat_manager.py', 'start', name])

        # Wait an hour before next cycle
        print('Sleeping for 1 hour...')
        time.sleep(3600)

if __name__ == '__main__':
    run()

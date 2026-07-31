#!/usr/bin/env python3
"""Monitor script for XLM paper trading strategies.
Runs an infinite loop checking strategy performance every hour.
It stops the two worst performing strategies, clones the two best strategies with
slightly tweaked thresholds, and starts the new clones.
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

STATE_FILE = Path('/opt/strategy_state.json')
STRATEGIES_DIR = Path('/opt/strategies')
TEMPLATE_REPO = 'file:///opt/template_repo'
MASTER_AGENT_SCRIPT = Path('/opt/master_agent/master-agent.py')
REVISION_TIMEOUT = 6000 # seconds allotted for the LLM to revise a clone before falling back

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

def compute_net_worth(strategy_name, state_entry, price):
    strategy_path = Path(state_entry['path'])
    state_path = strategy_path / 'state.json'
    if not state_path.exists():
        return -float('inf')
    try:
        data = json.load(state_path.open())
        usd = data.get('balance_usd', 0.0)
        xlm = data.get('balance_xlm', 0.0)
        return usd + xlm * price
    except Exception as e:
        print(f'Error reading state for {strategy_name}: {e}')
        return -float('inf')

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
            net = compute_net_worth(name, info, price)
            performances.append((name, net))
        performances.sort(key=lambda x: x[1], reverse=True)
        print('Strategy performances (net worth USD):')
        for name, net in performances:
            print(f'  {name}: {net:.2f}')
        # Stop worst two (if they are running)
        worst_two = performances[-2:]
        for name, _ in worst_two:
            info = state.get(name)
            if info and info.get('status') == 'running':
                print(f'Stopping worst strategy {name}')
                subprocess.run(['/opt/strat_manager.py', 'stop', name])
        # Clone best two and hand each clone to the master agent to revise before starting
        best_two = performances[:2]
        leaderboard = json.dumps({n: net for n, net in performances})
        for name, net in best_two:
            # create a new unique name (not derived from the parent's name, so it
            # doesn't keep growing across generations of clones-of-clones)
            new_name = f"clone_{uuid.uuid4().hex[:12]}"
            print(f'Cloning best strategy {name} as {new_name}')
            subprocess.run(['/opt/strat_manager.py', 'clone', new_name, TEMPLATE_REPO])
            parent_cfg = Path(state[name]['path']) / 'config.json'
            new_cfg = Path(STRATEGIES_DIR) / new_name / 'config.json'

            revised = False
            if MASTER_AGENT_SCRIPT.exists():
                try:
                    result = subprocess.run(
                        ['python3', str(MASTER_AGENT_SCRIPT), 'revise-strategy',
                         new_name, name, str(net), leaderboard, str(price)],
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

            # start the new clone
            subprocess.run(['/opt/strat_manager.py', 'start', new_name])
        # Wait an hour before next cycle
        print('Sleeping for 1 hour...')
        time.sleep(3600)

if __name__ == '__main__':
    run()

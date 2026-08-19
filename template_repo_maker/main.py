#!/usr/bin/env python3
"""Market-maker template strategy: decide where to rest a two-sided quote.

## Structure matters here -- read before editing

The module top level must contain ONLY imports, assignments, function/class definitions,
the docstring, and the `if __name__ == '__main__'` guard. That is exactly what
/opt/tools/maker_backtest.py::importability_report permits, and if this file steps outside
it, replay() silently stops importing quote() and falls back to the mechanical
config-genome rule instead -- so a revision's real logic becomes invisible to its own
fitness check while replay() still returns confident-looking numbers for the fallback.

`sys.path = sys.path + ['/opt/tools']` is written as a plain assignment on purpose:
`sys.path.append(...)` is a bare call expression, which is NOT on the whitelist. Do not
"tidy" it back.

The tick loop lives in `main()` under the `__main__` guard, not at top level, so importing
this module for a replay never starts it.

## Where strategy logic goes

`quote(book, state, config)` is what maker_backtest.py replays and what main() calls every
tick. It answers a maker's question -- WHERE do I rest, and HOW BIG -- and returns

    {'bid': (price_usd, size_usd) | None,
     'ask': (price_usd, size_usd) | None}      or None to pull both quotes.

It must stay pure and fast: the replay calls it once per recorded minute over days of
history, and it must not touch the network. Everything it needs is in `book`:

    book['bid'], book['ask'], book['mid']      the touch
    book['spread_bp']                          the current spread
    book['bids'], book['asks']                 the near ladder, [{'p','usd'}, ...]
    book['bid_depth_usd'], book['ask_depth_usd']

and everything about the strategy's own position is in `state`:

    state['balance_usd'], state['positions'], state['open_offers'],
    state['inventory_usd']                     the XLM leg at the mid. Resting offers are
                                               NOT added on top: nothing is reserved when
                                               a quote is placed, so positions already
                                               contains the XLM behind a resting ask.

The default rule rests just inside the current touch -- never closer to the mid than
`half_width_bp`, which acts as a FLOOR rather than as the quote distance -- and leans
against inventory: long inventory pushes both quotes down so the ask is likelier to fill
and the bid less so, and past `inventory_band_usd` the offending side stands down
entirely. Every knob lives in
config.json, not here, so mechanical mutation (DOMAIN.tweak_config) and every later
revision can find and adjust it without a code change. Inventing a genuinely new knob is
fine -- read it with `config.get('your_key', <sensible default>)`, never
`config['your_key']`, so a fresh template spawn that never set it still runs.

WHAT THE MEASUREMENT SAYS, so a revision does not have to rediscover it. Over 8 days of
recorded tape:

  * This book's real depth sits a few basis points behind a touch made of half-cent dust.
    A quote INSIDE the touch has nothing queued ahead of it and fills; a quote outside it
    queues behind thousands of dollars and effectively never fills. Wider is not safer
    here, it is just idle.
  * Anchoring to the touch beat anchoring to the mid at every floor tested at or below
    5 bp, best at a 3 bp floor: +$5.36 net over 8 days against +$1.99 for the best
    mid-anchored width. That is why the rule below is written the way it is.
  * The edge decays to zero if the quote takes more than ~10 seconds to reach the book.
  * Adverse selection ate 82-92% of gross spread capture at every width. Inventory
    management was worth more than the width.

See MAKER_PHASE1.md.

## Where execution happens -- nowhere in this file

/opt/tools/quote_executor.py::sync_quotes is the only thing that may place, replace or
cancel an offer, detect a fill, or move a balance. Call it once per tick with whatever
quote() returned. Do NOT reimplement any of it here, and do not call stellar_trader
directly: an offer outlives the process that placed it, so a main.py that manages its own
offers can leave real money resting after the strategy is culled, revised or demoted.
`domain_sdex_maker.can_execute_live` requires a call to `sync_quotes` by name before this
strategy may ever go live, and `check_smoke_state` fails a candidate that finishes with
offers still open.
"""
import json
import sys
import time
from pathlib import Path

# Plain assignment, deliberately. See the module docstring.
sys.path = sys.path + ['/opt/tools']

import market_recorder
import quote_executor

CONFIG_PATH = Path('config.json')
STATE_PATH = Path('state.json')

TICK_SECONDS = 30

START_USD = 1000.0          # matches maker_backtest.START_USD, so paper and replay agree


def load_config():
    try:
        with CONFIG_PATH.open() as f:
            return json.load(f)
    except Exception as e:
        print(f'could not read config.json ({e}); running with defaults')
        return {}


def load_state():
    try:
        with STATE_PATH.open() as f:
            return json.load(f)
    except Exception:
        return {'balance_usd': START_USD, 'balance_xlm': 0.0}


def save_state(state):
    try:
        with STATE_PATH.open('w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f'could not write state.json ({e})')


def quote(book, state, config):
    """Where to rest, how big. Pure and fast -- called once per backtest tick.

    Returns {'bid': (price, usd), 'ask': (price, usd)} with either side None to stand
    down on that side, or None to pull both quotes.
    """
    mid = book.get('mid')
    if not mid or mid <= 0:
        return None

    half_bp = float(config.get('half_width_bp', 0.0))
    size = float(config.get('quote_size_usd', 0.0))
    if half_bp <= 0 or size <= 0:
        # The template ships inert on purpose: DOMAIN.config_is_sane rejects this, and
        # DOMAIN.seed_config then builds a real genome from the recorded spread
        # distribution. A template that quoted a plausible default would make every
        # spawn a copy of one hand-picked point.
        return None

    inventory = float(state.get('inventory_usd', 0.0))
    band = float(config.get('inventory_band_usd', 250.0))
    skew_bp = float(config.get('inventory_skew_bp', 0.0))

    # Lean, don't cross: being long shifts BOTH quotes down, which makes the ask likelier
    # to fill and the bid less so, without ever paying the spread to reduce the position.
    lean = 0.0
    if band > 0 and skew_bp:
        lean = max(-1.0, min(1.0, inventory / band)) * skew_bp / 10000.0

    # ANCHORED TO THE TOUCH, WITH half_width_bp AS A FLOOR -- not at a fixed distance from
    # the mid. This is measured, not stylistic. The spread on this pair moves between about
    # 5 and 16 bp, so a quote at a fixed distance from the mid spends roughly half its life
    # OUTSIDE the touch, queued behind thousands of dollars and unable to fill at all.
    # Stepping just inside the current best bid/ask instead, and falling back to the
    # mid-anchored price only when the spread is too tight to allow it, captured 2.7x the
    # net edge over the same 8 days (+$5.36 against +$1.99). half_width_bp is what stops a
    # tightening spread from dragging the quote in to where capture no longer covers
    # adverse selection.
    improve_bp = float(config.get('improve_bp', 0.1))
    floor_bid = mid * (1 - half_bp / 10000.0 - lean)
    floor_ask = mid * (1 + half_bp / 10000.0 - lean)
    best_bid, best_ask = book.get('bid'), book.get('ask')
    bid = (min(best_bid * (1 + improve_bp / 10000.0), floor_bid)
           if best_bid else floor_bid, size)
    ask = (max(best_ask * (1 - improve_bp / 10000.0), floor_ask)
           if best_ask else floor_ask, size)

    if band > 0 and inventory > band:
        bid = None          # too long to keep bidding
    if band > 0 and inventory < -band:
        ask = None
    return {'bid': bid, 'ask': ask}


def current_book():
    """The latest recorded book row as the dict quote() expects, or None.

    market_recorder.tail(1), never a live order-book fetch: that module is the single
    writer and ten strategies fetching their own book every 30s is a rate-limit incident.
    """
    rows = market_recorder.tail(1)
    if not rows:
        return None
    row = rows[-1]
    if not row.get('dex_mid'):
        return None
    # spread_bp is derived rather than read straight through: the recorder stores None for
    # a LOCKED book (bid == ask), and a strategy's own `float(book.get('spread_bp', 0.0))`
    # raises on that None -- the key is present, so the default never applies -- which
    # stands the maker down for the tick. maker_backtest._spread_bp does the same for the
    # already-recorded rows a replay reads; kept inline here so quote() costs no import.
    spread_bp = row.get('spread_bp')
    if spread_bp is None and row.get('dex_bid') and row.get('dex_ask'):
        spread_bp = round((row['dex_ask'] - row['dex_bid']) / row['dex_mid'] * 10000.0, 2)
    return {'bid': row.get('dex_bid'), 'ask': row.get('dex_ask'),
            'mid': row.get('dex_mid'), 'spread_bp': spread_bp,
            'bids': row.get('bids') or [], 'asks': row.get('asks') or [],
            'bid_depth_usd': row.get('bid_depth_usd'),
            'ask_depth_usd': row.get('ask_depth_usd'),
            'ts': row.get('ts'), '_row': row}


def main():
    config = load_config()
    agent_name = config.get('name', 'unnamed')
    state = load_state()
    print(f"Agent {agent_name} starting, "
          f"{state.get('fills_total', 0)} fill(s) so far", flush=True)

    # Persist once before the first tick. monitor.py's smoke test reverts any revision
    # that hasn't written a readable state.json within SMOKE_TEST_SECONDS, and a first
    # tick can outlast that on a slow interpreter start.
    save_state(state)

    try:
        while True:
            book = current_book()
            if book is None:
                print('no recorded book yet; standing down this tick', flush=True)
                time.sleep(TICK_SECONDS)
                continue
            state['inventory_usd'] = quote_executor.inventory_usd(state, book['mid'])
            try:
                decision = quote(book, state, config)
            except Exception as e:
                print(f'quote() raised {type(e).__name__}: {e}; standing down', flush=True)
                decision = None
            state = quote_executor.sync_quotes(agent_name, decision, state, config,
                                               book=book['_row'])
            save_state(state)
            time.sleep(TICK_SECONDS)
    finally:
        # A maker that dies with quotes resting is the one failure mode that loses money
        # while nothing is running. This is best-effort -- SIGKILL skips it -- which is
        # exactly why MAX_OFFER_AGE_S exists outside this file as well.
        state = quote_executor.stand_down(agent_name, state)
        save_state(state)


if __name__ == '__main__':
    main()

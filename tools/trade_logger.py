#!/usr/bin/env python3
"""Utility to log simulated trades for agents.
Each agent gets its own log file under /opt/trades/<agent_name>.log.
Log format: JSON line with fields: timestamp, agent, action, price, amount_usd, amount_xlm, balance_usd, balance_xlm
"""
import json
import os
import time
from pathlib import Path

from stellar_trader import submit_trade

import assets as _assets
import friction as _friction
import portfolio as _portfolio

BASE_DIR = Path('/opt/trades')
BASE_DIR.mkdir(parents=True, exist_ok=True)

LIVE_FLAG = Path('live.flag')
CONFIG_FILE = Path('config.json')

_INF = float('inf')


def _spec_of(asset):
    """Canonical spec from whatever a caller passed: 'XLM', 'CODE:ISSUER', or a dict."""
    return _assets.normalize(asset)


def _declared_specs():
    """Specs this strategy's own config.json permits it to hold, plus XLM.

    Read from the current working directory, the same assumption LIVE_FLAG already
    makes -- strat_manager starts every strategy with cwd set to its own directory. On
    any failure this returns None meaning "cannot tell", and the caller allows the
    trade: a missing/unreadable config must not silently disable a running strategy's
    XLM leg. This is defense in depth, not the security boundary; monitor's admission
    pass and stellar_trader's enforcement are.
    """
    try:
        with open(CONFIG_FILE) as f:
            return _portfolio.declared_specs(json.load(f))
    except Exception:
        return None

def _live_fields(side, requested_usd, result):
    """submit_trade's result, normalized into the log line's `live` sub-object.

    The paper notional and the real fill are not the same number: stellar_trader clamps
    every real order to MAX_TRADE_USD, the remaining daily budget and claudio's actual
    on-chain balance, so a strategy promoted on a paper record of N-dollar trades can be
    filling a fraction of that -- or, as observed on 2026-08-03, nothing at all, 343
    times in a row. That ratio used to be printed to stdout and discarded, which meant
    the live result could neither confirm nor refute the paper result that justified the
    promotion. Recording it is the whole point.

    Defensive on purpose: this runs inside the money path's logging step, after state has
    already been mutated and the order already submitted, so a malformed result must
    degrade to a recorded refusal rather than raise.
    """
    try:
        result = result or {}
        filled = float(result.get('amount_usd') or 0.0)
    except Exception:
        result, filled = {}, 0.0
    try:
        requested = float(requested_usd)
    except Exception:
        requested = 0.0
    return {
        'side': side,
        'requested_usd': requested,
        'submitted': bool(result.get('submitted')),
        'amount_usd': filled,
        'tx_hash': result.get('tx_hash'),
        'reason': result.get('reason'),
        # None rather than 0 for a zero-size request: "no ratio is defined" is not the
        # same finding as "asked for money and got none".
        'size_ratio': round(filled / requested, 6) if requested > 0 else None,
    }


def record_trade(agent_name, action, price, amount_usd, amount_xlm, balance_usd, balance_xlm,
                 *, asset='XLM', asset_issuer=None, amount_asset=None, balance_asset=None,
                 live=None, fill_price=None, friction_bp=None, short=None):
    """Append one JSON line to /opt/trades/<agent_name>.log.

    The seven positional parameters and their meanings are unchanged -- strategies
    import this directly and monitor.trade_stats reads the file, so the v1 shape is
    load-bearing. The multi-asset fields are keyword-only additions:

      asset/asset_issuer/asset_spec  which asset the trade was in
      amount_asset/balance_asset     quantities in that asset's own units
      live                           what the real order did, or None -- see below

    For an XLM trade the new fields simply restate the old ones, so the line is
    indistinguishable from v1 to any existing reader. For a non-XLM trade, `amount_xlm`
    is 0.0 and `balance_xlm` is passed through unchanged, which keeps the XLM ledger in
    the log coherent rather than mixing units into a column named after XLM.

    `live` is written as a `live` key *only when a real submission was attempted*, so a
    paper-only line -- every line of every non-live strategy -- stays byte-identical to
    what this wrote before. Its presence is therefore the version marker, and a more
    useful one than `schema` (which stays 2, since none of the fields above changed
    meaning, type or presence): it distinguishes "never attempted" from "attempted and
    refused", which is exactly the distinction live-vs-paper reporting turns on.

    `fill_price`/`friction_bp` are the same kind of additive, presence-is-the-marker
    field (2026-08-03). `price` keeps its v1 meaning exactly -- the reference price the
    decide step saw -- so monitor.trade_stats, live_report and every strategy reading
    these logs are unaffected. `fill_price` is what the trade actually transacted at
    after crossing the book; the difference between them is the cost that used to be
    silently zero. Absent on a line written before friction existed, which is precisely
    how to tell the two eras apart when comparing P&L across the cutover.

    `short`: same presence-is-the-marker convention, True only for a sell that drew on
    the borrow facility (see SHORTING_PLAN.md). Absent on every line written before
    shorting existed and on every non-shorting line since.
    """
    spec = _assets.canonical(asset, asset_issuer) if asset_issuer else _spec_of(asset)
    code, issuer = _assets.parse(spec)
    entry = {
        "timestamp": time.time(),
        "agent": agent_name,
        "action": action,  # "buy" or "sell"
        "price": price,
        "amount_usd": amount_usd,
        "amount_xlm": amount_xlm,
        "balance_usd": balance_usd,
        "balance_xlm": balance_xlm,
        "schema": 2,
        "asset": code,
        "asset_issuer": issuer,
        "asset_spec": spec,
        "amount_asset": amount_xlm if amount_asset is None else amount_asset,
        "balance_asset": balance_xlm if balance_asset is None else balance_asset,
    }
    if live is not None:
        entry["live"] = live
    if fill_price is not None:
        entry["fill_price"] = fill_price
        entry["friction_bp"] = friction_bp
    if short:
        entry["short"] = True
    log_path = BASE_DIR / f"{agent_name}.log"
    with open(log_path, 'a') as f:
        f.write(json.dumps(entry) + "\n")


def execute_trade(agent_name, action, side, price, requested_usd, state, *,
                  asset='XLM', is_live=None, allow_shorting=False):
    """Mutate `state` for one trade, log it, and submit it live if this strategy is live.

    This is the single place strategy execution mechanics live, so that a strategy's
    main.py only needs to decide *whether* to trade, not how to safely mutate balances,
    log the result, or gate/submit a real order. `main.py` should call this instead of
    reimplementing any of it.

    agent_name: used for the log filename (same as record_trade).
    action: free-form label for the log (e.g. 'buy', 'sell', 'sell_stoploss') -- never
        used for control flow, just passed through to record_trade.
    side: normalized 'buy' | 'sell' -- the only thing used to decide which balance to
        mutate and what to pass to stellar_trader.submit_trade.
    price: the current *reference* price (the CEX aggregate for XLM, the mark for
        anything else). The trade does not fill here: friction.fill_price adjusts it by
        the live half-spread first, since a buy lifts the ask and a sell hits the bid.
        `price` is still what gets logged in the `price` field, unchanged.
    requested_usd: how much USD notional the caller wants to trade -- a request, not a
        guarantee. Clamped here to what's actually affordable/sellable, so a decide step
        can never overdraft USD or oversell the asset no matter what it asks for.
    state: the balances dict the caller's loop threads through; normalized to the
        multi-asset shape, mutated in place, and returned. `balance_xlm` is kept in
        sync as a mirror of the XLM position, so callers that only know about the old
        two-key shape keep working unchanged.
    asset: which asset to trade -- 'XLM' (default), 'CODE:ISSUER', or a
        {'code','issuer'} dict. Keyword-only with a default, so every existing
        positional call site is unaffected. The trade is refused unless this strategy's
        own config.json declares the asset.
    is_live: if None (the normal case), determined by checking for a live.flag file in
        the current working directory -- callers (main.py) run with cwd set to their own
        strategy directory, so this resolves correctly without the caller needing to
        check it itself. Pass explicitly to override (e.g. for tests).
    allow_shorting: XLM-only. When True and side is 'sell', a sell beyond what's held
        draws against state['borrowed_xlm'] (capped at portfolio.MAX_BORROWED_XLM)
        instead of clamping to the held amount. A later buy always repays any
        outstanding borrow before it grows the real position, regardless of this flag --
        the flag only gates *opening* a short, not unwinding one. See SHORTING_PLAN.md.

    No-ops (returns state unchanged, no log line, no live submission) if the clamped
    trade size is <= 0, or if `price`/`requested_usd` is not a usable positive number.
    """
    # `price` is divided by twice below, so a zero/negative/nan price is either a
    # ZeroDivisionError or a silently negative fill inside the one function every
    # strategy depends on. get_price() returns None on total failure and the XLM feed is
    # a liquid CEX aggregate, so this was hard to hit -- but a thin DEX order book can
    # legitimately return 0, an empty book, or a nan mid, which makes it reachable.
    # Note nan needs its own check: `nan <= 0` is False, so it slips past a plain
    # comparison and then poisons every balance it touches.
    if price is None or price != price or price <= 0 or price in (_INF, -_INF):
        print(f'[{agent_name}] refusing trade: unusable price {price!r}')
        return state
    if requested_usd is None or requested_usd != requested_usd:
        print(f'[{agent_name}] refusing trade: unusable size {requested_usd!r}')
        return state
    if side not in ('buy', 'sell'):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    try:
        spec = _spec_of(asset)
    except _assets.AssetError as e:
        print(f'[{agent_name}] refusing trade: {e}')
        return state

    # A strategy may only trade what its own config declares. Cheap, and it stops a
    # revision from quietly accumulating a position in something monitor never admitted.
    declared = _declared_specs()
    if declared is not None and spec not in declared:
        print(f'[{agent_name}] refusing trade in undeclared asset '
              f'{_assets.display(spec)}; config declares {sorted(declared)}')
        return state

    state = _portfolio.normalize_state(state)
    code = _assets.parse(spec)[0]
    is_native = _assets.is_native(spec)

    # Every fill crosses the book. Until 2026-08-03 this function bought and sold at the
    # same `price`, which made trading free and made the whole population evolve toward
    # churning: the leader was turning over $5,757 for a $23.33 gain -- a 40.5 bp edge
    # against a real 12.6 bp XLM book it never paid, and against 148 bp books on the
    # extra assets it kept being handed. stellar_trader submits path-payment-strict-send
    # swaps, which TAKE liquidity, so a buy lifts the ask and a sell hits the bid.
    # friction.fill_price never raises and never returns zero cost; see its docstring for
    # why the failure mode is to charge the floor rather than to exempt.
    half = _friction.half_spread(spec)
    fill = _friction.fill_price(spec, side, price, h=half)
    friction_bp = round(half * 10000, 3)

    short = False  # set True below only for a sell that draws on the borrow facility
    if side == 'buy':
        actual_usd = min(requested_usd, state['balance_usd'])
        if actual_usd <= 0:
            return state
        amount_asset = actual_usd / fill
        state['balance_usd'] -= actual_usd
        trade_usd = actual_usd
        if is_native:
            # Repay any outstanding short before growing the real position -- always,
            # not gated on allow_shorting, so flipping the flag off doesn't strand a
            # borrow that can no longer be covered.
            borrowed = state.get('borrowed_xlm', 0.0)
            repay = min(amount_asset, borrowed)
            if repay > 0:
                state['borrowed_xlm'] = borrowed - repay
                proceeds = state.get('short_proceeds_usd', 0.0)
                state['short_proceeds_usd'] = max(0.0, proceeds - repay * fill)
                amount_asset -= repay
        if amount_asset > 0:
            _portfolio.add_amount(state, spec, amount_asset)
    else:
        held = _portfolio.get_amount(state, spec)
        if is_native and allow_shorting:
            borrowed = state.get('borrowed_xlm', 0.0)
            headroom = max(0.0, _portfolio.MAX_BORROWED_XLM - borrowed)
        else:
            headroom = 0.0
        actual_asset = min(requested_usd / fill, held + headroom)
        if actual_asset <= 0:
            return state
        trade_usd = actual_asset * fill
        from_held = min(actual_asset, held)
        from_borrow = actual_asset - from_held
        _portfolio.add_amount(state, spec, -from_held)
        if from_borrow > 0:
            state['borrowed_xlm'] = state.get('borrowed_xlm', 0.0) + from_borrow
            state['short_proceeds_usd'] = state.get('short_proceeds_usd', 0.0) + from_borrow * fill
            short = True
        state['balance_usd'] += trade_usd
        amount_asset = actual_asset

    balance_asset = _portfolio.get_amount(state, spec)
    print(f"[{agent_name}] {'Bought' if side == 'buy' else 'Sold'} "
          f"{amount_asset:.4f} {code} at ${fill:.6f} "
          f"(ref ${price:.6f}, {friction_bp:.1f}bp)")

    if is_live is None:
        is_live = LIVE_FLAG.exists()

    # The paper line is written *after* the live submission so that it can carry the real
    # fill, which was previously printed and thrown away. try/finally, deliberately:
    #   - submit_trade is NOT wrapped in `except`. An order that raises has always killed
    #     the strategy process, and swallowing that here would change behavior on the
    #     money path to hide an error, which is the wrong trade in this direction.
    #   - `finally` means the paper line is still written exactly once on every path,
    #     including the raising one, where it used to be written before submission and
    #     would otherwise now be lost.
    # Nothing above this point moved: the clamping and the state mutation are untouched.
    live_record = None
    try:
        if is_live:
            # Every admitted asset, not just XLM, since 2026-08-03. This branch used to
            # refuse anything non-native on the grounds that submit_trade had no asset
            # parameter and that trustlines, per-asset caps and a multi-leg wind_down were
            # "none of which are in place" -- all three had since been built and left
            # unreachable, because this was the only caller and it never passed `asset`.
            #
            # Nothing here decides whether the trade is allowed. submit_trade re-checks
            # admission, trustline, per-asset and aggregate caps, stuck legs and the halt
            # file, and refuses with a reason that lands in the log line below.
            result = submit_trade(side, trade_usd, asset=spec, short=short)
            print(f"[{agent_name}] LIVE: submit_trade({side!r}, {trade_usd}, "
                  f"asset={spec!r}, short={short!r}) -> {result}")
            live_record = _live_fields(side, trade_usd, result)
    finally:
        try:
            record_trade(
                agent_name, action, price, trade_usd,
                # amount_xlm/balance_xlm keep their v1 meaning: the XLM leg only.
                amount_asset if is_native else 0.0,
                state['balance_usd'], state['balance_xlm'],
                asset=spec, amount_asset=amount_asset, balance_asset=balance_asset,
                live=live_record, fill_price=fill, friction_bp=friction_bp, short=short)
        except Exception as e:
            # A logging failure must not be able to take down a strategy that has already
            # traded; the balances in `state` are the authoritative record either way.
            print(f'[{agent_name}] WARNING: trade log write failed: {e}')

    return state

#!/usr/bin/env python3
"""Multi-asset balances in state.json, and the one back-compat shim for the old shape.

## Why this module exists

Every strategy used to hold exactly two things: USD and XLM, spelled `balance_usd` and
`balance_xlm` in state.json. Multi-asset strategies hold USD plus XLM plus up to two
discovered Stellar assets, which needs a map rather than a fixed pair of keys.

There are ~92 strategy directories on disk, ~10 of them running at any time, plus
score.py, leaderboard.py, monitor.py's smoke test and backtest.py all reading
`balance_xlm` directly. So the migration rule is:

    `balance_xlm` is retained FOREVER as a mirror of positions['XLM']['amount'],
    re-derived on every write.

That one decision means every existing reader keeps working untouched, and no strategy
has to be restarted or rewritten to participate. Do not "clean up" `balance_xlm` later:
it is not legacy debris, it is the compatibility contract.

## Schema

v1 (what's on disk today):

    {"balance_usd": 1013.44, "balance_xlm": 0.0}

v2:

    {"schema_version": 2,
     "balance_usd": 1013.44,
     "balance_xlm": 0.0,                       <- mirror, never the source of truth
     "positions": {
       "XLM":         {"code": "XLM",  "issuer": null,    "amount": 0.0},
       "AQUA:GBNZ…":  {"code": "AQUA", "issuer": "GBNZ…", "amount": 1234.5}}}

Strategies also keep private keys in state.json (`price_history`, `last_buy_price`,
`prev_short_sma`, …). Those are preserved untouched -- normalize_state only ever *adds*
`positions`/`schema_version` and rewrites `balance_xlm`.

## Migration needs no script and no restarts

- A strategy that calls `execute_trade` is normalized on its next trade and persists the
  result through its own `json.dump(state)`. Zero main.py changes.
- A strategy that never trades keeps a v1 file; readers (score.py, the smoke test)
  normalize **in memory and never write back** -- writing would race the strategy's own
  30-second state write.
- The ~10 strategies that bypass `execute_trade` with inline `record_trade` never
  migrate. That's correct: they are XLM-only by construction.

## Authority when v1 and v2 fields disagree

`positions` wins, and `balance_xlm` is re-derived from it. `positions` is the ledger that
matches the trade log, so a strategy that mutates `balance_xlm` directly (which the
revision prompt forbids) loses the direct mutation rather than silently corrupting a
position that real trades were logged against.
"""
import assets

SCHEMA_VERSION = 2

# XLM is a permanent base leg and is never listed in config's `assets`; this caps only
# the *additional* discovered assets. Enforced here and re-enforced in monitor.py.
MAX_EXTRA_ASSETS = 2

# Paper-side ceiling on the XLM short facility (see SHORTING_PLAN.md). Not config-driven,
# same as MAX_EXTRA_ASSETS being a hard module constant rather than revision-widenable.
MAX_BORROWED_XLM = 6000.0


def _as_float(value, default=0.0):
    """Coerce to float without ever raising. Non-finite values become `default`, since
    a nan/inf balance would poison every downstream comparison silently."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float('inf'), float('-inf')):
        return default
    return result


def _blank_position(code, issuer):
    return {'code': code, 'issuer': issuer, 'amount': 0.0}


def normalize_state(state):
    """Return `state` upgraded to v2, in place. Idempotent, and never raises.

    Safe to call on a v1 state, a v2 state, a partially-written state, or garbage --
    anything unparseable is replaced with a zero balance rather than propagating an
    exception into a trading loop or into monitor's scoring pass.
    """
    if not isinstance(state, dict):
        state = {}

    state['balance_usd'] = _as_float(state.get('balance_usd'))
    # Short-side ledger, deliberately separate from positions['XLM']: add_amount hard-
    # clamps at zero and that invariant can't be touched without breaking every existing
    # long-only reader. See SHORTING_PLAN.md.
    state['borrowed_xlm'] = _as_float(state.get('borrowed_xlm'))

    raw = state.get('positions')
    positions = raw if isinstance(raw, dict) else {}

    rebuilt = {}
    for key, entry in positions.items():
        if not isinstance(entry, dict):
            continue
        # Trust the entry's own code/issuer over its dict key, then re-derive the key --
        # so a hand-edited or LLM-written state.json can't smuggle in a position under a
        # key that doesn't match the asset it actually names.
        try:
            spec = assets.canonical(entry.get('code'), entry.get('issuer'))
        except assets.AssetError:
            try:
                spec = assets.normalize(key)
            except assets.AssetError:
                continue  # unidentifiable position; drop rather than guess
        code, issuer = assets.parse(spec)
        amount = _as_float(entry.get('amount'))
        if spec in rebuilt:
            rebuilt[spec]['amount'] += amount  # duplicate keys for one asset: merge
        else:
            rebuilt[spec] = {'code': code, 'issuer': issuer, 'amount': amount}

    if assets.NATIVE not in rebuilt:
        # First upgrade from v1: seed the XLM leg from the legacy field. On a v2 state
        # that has somehow lost its XLM entry this recovers it the same way.
        rebuilt[assets.NATIVE] = _blank_position(assets.NATIVE, None)
        rebuilt[assets.NATIVE]['amount'] = _as_float(state.get('balance_xlm'))

    state['positions'] = rebuilt
    state['schema_version'] = SCHEMA_VERSION
    sync_legacy(state)
    return state


def sync_legacy(state):
    """Re-derive `balance_xlm` from positions['XLM']. Call after every mutation."""
    position = state.get('positions', {}).get(assets.NATIVE)
    state['balance_xlm'] = _as_float(position.get('amount')) if position else 0.0


def positions(state):
    """The position map, normalizing first. {spec: {'code','issuer','amount'}}."""
    return normalize_state(state)['positions']


def get_amount(state, spec):
    """Held amount of `spec`. 0.0 if the asset isn't held or the spec is malformed."""
    try:
        spec = assets.normalize(spec)
    except assets.AssetError:
        return 0.0
    position = normalize_state(state)['positions'].get(spec)
    return _as_float(position.get('amount')) if position else 0.0


def add_amount(state, spec, delta):
    """Add `delta` (may be negative) to the held amount of `spec`, creating the leg if
    needed, and keep `balance_xlm` in sync. Returns the new amount.

    Clamps at zero: the callers that matter (execute_trade, backtest) already clamp the
    trade size against the held amount, so a negative result here means a rounding
    artifact rather than a real overdraft, and a negative position would silently
    corrupt every net-worth calculation downstream.
    """
    spec = assets.normalize(spec)
    code, issuer = assets.parse(spec)
    state = normalize_state(state)
    position = state['positions'].setdefault(spec, _blank_position(code, issuer))
    position['amount'] = max(0.0, _as_float(position['amount']) + _as_float(delta))
    sync_legacy(state)
    return position['amount']


def mark_price(mark):
    """Scalar USD unit price from a mark entry, or None.

    A mark may be a bare float, or a {'price', 'bids'} dict carrying the asset's bid
    ladder so scoring can depth-cap a position without re-fetching per strategy. Both
    forms are accepted everywhere a mark is read, so callers that only need a price never
    have to care which they were handed.
    """
    if isinstance(mark, dict):
        mark = mark.get('price')
    return _as_float(mark, default=0.0) if mark is not None else None


def realizable_value(amount, mark):
    """USD the position is really worth: depth-capped when a bid ladder is available,
    otherwise amount x unit price. Returns None if the asset cannot be priced at all."""
    price = mark_price(mark)
    if price is None or price <= 0:
        return None
    if isinstance(mark, dict) and mark.get('bids'):
        try:
            import dex_price
            return dex_price.realize_against_bids(amount, mark['bids'])
        except Exception:
            pass
    return amount * price


def net_worth(state, marks):
    """Mark-to-market net worth. Returns (net_worth, unpriced_specs).

    `marks` maps spec -> USD price per unit. This is the *raw* figure -- no haircuts.
    score.py applies its own haircuts on top, deliberately separately, so that the
    smoke test's "is this strategy solvent" check and the leaderboard's "which strategy
    should we clone" ranking can't drift into meaning the same thing.

    A held position with no mark is reported in `unpriced_specs` and contributes
    **zero**. Callers decide what that means: the smoke test treats it as "can't
    confirm solvent", score.py falls back to a last-known-good mark up to an age limit
    and then to zero, because an asset that suddenly can't be priced is what a rug looks
    like and marking it at its last good price would reward holding one.

    borrowed_xlm (SHORTING_PLAN.md) is a debt, not a position, so it never appears in
    `positions` -- it is subtracted here too, raw (no haircut, matching this function's
    own no-haircut contract; score.py applies UNREALIZED_HAIRCUT on top separately). A
    smoke-tested allow_shorting strategy that opened a short must still be judged
    solvent net of what covering it would cost, not on the sale proceeds alone.
    """
    state = normalize_state(state)
    total = state['balance_usd']
    unpriced = []
    for spec, position in state['positions'].items():
        amount = _as_float(position['amount'])
        if amount <= 0:
            continue
        mark = marks.get(spec) if isinstance(marks, dict) else None
        value = realizable_value(amount, mark) if mark is not None else None
        if value is None:
            unpriced.append(spec)
            continue
        total += value

    borrowed = state.get('borrowed_xlm', 0.0)
    if borrowed > 0:
        mark = marks.get(assets.NATIVE) if isinstance(marks, dict) else None
        price = mark_price(mark) if mark is not None else None
        if price is None:
            unpriced.append('XLM:short-liability')
        else:
            total -= borrowed * price

    return total, unpriced


def held_specs(state, include_native=True):
    """Specs with a strictly positive balance -- what actually needs a price."""
    result = [
        spec for spec, position in positions(state).items()
        if _as_float(position['amount']) > 0
    ]
    if not include_native:
        result = [s for s in result if s != assets.NATIVE]
    return result


def assets_from_config(config):
    """The validated extra assets declared by a strategy's config.json. Never raises.

    Returns a list of at most MAX_EXTRA_ASSETS dicts:
        {'code', 'issuer', 'spec', 'buy_below', 'sell_above', 'trade_amount_usd'}

    Anything malformed is dropped silently rather than failing the whole config, because
    the caller is usually either a live trading loop (which must keep running on its XLM
    leg) or monitor's admission gate (which drops bad legs and keeps the revision).

    XLM is never listed here -- it is a permanent base leg carried by config's top-level
    buy_below/sell_above/trade_amount_usd. That's what makes every pre-multi-asset
    config a valid config with an implicit empty asset list.

    Deduped by spec *and* by code: two different issuers both calling themselves USDC in
    one strategy is unmanageable for position tracking and is a hallmark of an
    impersonation, so the second one loses.
    """
    if not isinstance(config, dict):
        return []
    declared = config.get('assets')
    if not isinstance(declared, list):
        return []

    default_size = _as_float(config.get('trade_amount_usd'), default=10.0)
    result = []
    seen_specs = set()
    seen_codes = set()

    for entry in declared:
        if len(result) >= MAX_EXTRA_ASSETS:
            break
        if not isinstance(entry, dict):
            continue
        code = entry.get('code')
        issuer = entry.get('issuer')
        try:
            spec = assets.canonical(code, issuer)
        except assets.AssetError:
            continue
        if assets.is_native(spec):
            continue  # XLM is the base leg, never an "extra" asset
        if spec in seen_specs or code.upper() in seen_codes:
            continue
        seen_specs.add(spec)
        seen_codes.add(code.upper())
        result.append({
            'code': code,
            'issuer': issuer,
            'spec': spec,
            'buy_below': _as_float(entry.get('buy_below'), default=0.0),
            'sell_above': _as_float(entry.get('sell_above'), default=0.0),
            'trade_amount_usd': _as_float(
                entry.get('trade_amount_usd'), default=default_size),
        })
    return result


def declared_specs(config):
    """Every spec a strategy is allowed to hold: its extra assets plus XLM."""
    return {a['spec'] for a in assets_from_config(config)} | {assets.NATIVE}

#!/usr/bin/env python3
"""Real-money XLM/USDC trading on Stellar pubnet using the `claudio` identity.

This module is the actual safety boundary that plan describes: MAX_TRADE_USD
and MAX_DAILY_USD live only here, are never expressed as caller-supplied parameters, and
are enforced regardless of what a (possibly LLM-revised) strategy asks for.

Shells out to the `stellar` CLI for signing/submission, same shell-out pattern as
reflector_oracle.py, and to Horizon's REST API for balance queries — classic XLM/USDC
balances live in account trustlines, not a Soroban contract, so `contract invoke` (what
reflector_oracle.py uses) doesn't apply here.

Confirmed live against `claudio`
(GBTFQJ6VARJYI2C6JLPUXQ4CAKRNJF3KEYXXJ5T74DV47RSSNIJCH5VM), via a plain Horizon GET, not
assumed:
- already holds a USDC trustline - issuer GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN,
  the well-known Circle-issued USDC on pubnet.
- currently funded with ~27.9 XLM and ~14.0 USDC (also holds some BLND, irrelevant here).

Trading itself uses a self-payment `path-payment-strict-send` (source == destination ==
claudio's own address) - the standard way to take a market-order-like swap off the
Stellar DEX orderbook from the CLI, as opposed to manage-buy-offer/manage-sell-offer,
which post a *resting* limit order that may not fill immediately or in full.

CAUTION: submit_trade()/wind_down() sign and submit real pubnet transactions.
Exercise them manually from a REPL with MAX_TRADE_USD
this low before wiring them into monitor.py/main.py - running this file directly
(`python3 stellar_trader.py`) only prints a read-only status report, it does not trade.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import requests

from price_feed import get_price

_IDENTITY = "claudio"
_NETWORK = "pubnet"
_HORIZON = "https://horizon.stellar.org"
_TIMEOUT = 30
_TX_TIMEOUT = 60

# USDC is the settlement asset: every trade is denominated against it and wind_down
# flattens back into it. It is not a "tradeable asset" in the multi-asset sense and is
# never something a strategy selects.
_USDC_CODE = "USDC"
_USDC_ISSUER = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
_USDC_ASSET = f"{_USDC_CODE}:{_USDC_ISSUER}"
_USDC_SPEC = _USDC_ASSET

# --- safety caps -------------------------------------------------------------------
# All module-level, none caller-supplied, none read from any strategy's config.json.
# submit_trade now takes an `asset`, but it takes it as a *request*: the asset must
# already have been admitted by monitor, and every cap below is applied on top
# regardless of what the caller asks for. Nothing here can be widened by a revision --
# only by a human editing this file, which monitor.check_boundary_integrity() will
# notice and halt live trading over until it is reviewed and re-baselined.
MAX_TRADE_USD = 4.0                    # per trade, any asset
MAX_DAILY_USD = 99999.0                # per 24h across ALL assets combined
MAX_TRADE_USD_NONBASE = 0.50           # per trade, non-XLM only
MAX_DAILY_USD_PER_ASSET = 4.0          # per 24h per (code, issuer)
MAX_POSITION_USD_PER_ASSET = 4.0       # mark-to-market cap on any one non-XLM leg
MAX_TOTAL_NONBASE_EXPOSURE_USD = 8.0   # across every non-XLM leg at once
MAX_OPEN_NONBASE_ASSETS = 2            # matches the strategy-side limit
MAX_SYSTEM_TRUSTLINES = 3              # USDC + 2 discovered. BLND predates this system.
MIN_XLM_OPERATING_BUFFER = 1.0         # spendable XLM that must survive a new trustline
MAX_STUCK_USD = 2.0                    # total unsellable notional before a full halt
_VERIFY_TTL = 900                      # re-verification cache in the hot path

_SLIPPAGE = 0.01  # dest-min tolerance on path payments
_XLM_DUST = 0.5  # wind_down considers the position flat below this
_BASE_RESERVE_XLM = 0.5  # current Stellar protocol base reserve per subentry
# Confirmed live (Rollout phase 1 REPL test): spending exactly (balance - reserve) of
# native XLM fails on submission with a real Underfunded error, because the tx fee is
# also deducted from the same native balance before the payment operation executes —
# so the reserve alone isn't enough headroom. 100x the default 100-stroop fee for
# margin against fee bumps across a chunked sequence.
_FEE_BUFFER_XLM = 0.01
_MAX_WIND_DOWN_CHUNKS_PER_CALL = 20  # safety bound; remainder retried next monitor.py cycle
# Spendable XLM that wind_down() will not sell. Not a safety cap like the ones at the top
# of this file -- it exists because wind_down's "sell everything" premise drains the
# account to its protocol floor, leaving _spendable_xlm() at 0.0 and the sell leg dead
# until a human funds it again (observed 2026-08-01 16:09 and 2026-08-02 17:31, both
# times immediately after a leader change). Sized so the account can still open every
# trustline it is allowed to and keep the operating buffer on top.
WIND_DOWN_RESERVE_XLM = MIN_XLM_OPERATING_BUFFER + MAX_OPEN_NONBASE_ASSETS * _BASE_RESERVE_XLM


def _paper_only():
    """True when this process must never sign or submit a real transaction.

    monitor.py's smoke test runs a candidate main.py for real, in a throwaway copy, with
    PAPER_ONLY=1 in its environment. That copy already has live.flag removed, but that
    guarantee depends on remembering to strip the file every time the copy logic
    changes. This is the structural version: a process started with PAPER_ONLY set
    cannot submit, whatever its cwd contains.

    Checked inside submit_trade/wind_down rather than at import time so it cannot be
    defeated by importing the module before the variable is set.
    """
    return bool(os.environ.get('PAPER_ONLY'))

BASE_DIR = Path('/opt/trades')
BASE_DIR.mkdir(parents=True, exist_ok=True)
_LEDGER_PATH = BASE_DIR / '.pubnet_ledger.json'
_LIVE_STRATEGY_PATH = Path('/opt/live_strategy.json')
# Written by monitor's admission pass; read (never written) here.
_VERIFIED_ASSETS_PATH = BASE_DIR / '.verified_assets.json'
# Trustlines this system opened, so remove_trustline only ever closes its own.
_TRUSTLINES_PATH = BASE_DIR / '.trustlines.json'
# Legs that could not be sold. Their existence blocks new non-XLM buys.
_STUCK_PATH = BASE_DIR / '.stuck_positions.json'
# Operator-visible kill switch. While this exists nothing trades, including XLM.
_HALT_PATH = BASE_DIR / '.live_halt'

_verify_cache = {}


def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write_json(path, value):
    try:
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(value, indent=2))
        tmp.replace(path)
    except Exception as e:
        print(f'[stellar_trader] could not write {path}: {e}')


def _spec(code, issuer=None):
    """Canonical 'XLM' or 'CODE:ISSUER'. Raises on a malformed pair."""
    import assets
    return assets.canonical(code, issuer)


def _normalize_asset(asset):
    """Canonical spec from any accepted caller form: 'XLM', 'CODE:ISSUER', or a
    {'code','issuer'} dict. Raises on anything malformed."""
    import assets
    return assets.normalize(asset)


def _is_native(spec):
    return spec == 'XLM'


def _sep11(spec):
    import assets
    return assets.sep11(spec)


def _verified_asset(code, issuer):
    """The enforcement gate: may real money move into this asset right now?

    Returns the admission record, or None.

    This is the third and last of the three verification gates, and the only one that
    cannot be skipped. The first (asset_discovery.verify_asset) is LLM-invoked and
    therefore advisory. The second (monitor._sanitize_assets) is thorough but is a
    snapshot -- an asset admitted on Monday can be rugged on Tuesday while a strategy
    still holds it. So this re-reads monitor's registry AND does one cheap live check,
    rather than trusting the registry alone.

    It deliberately does not repeat the full five-source evidence sweep: that costs
    several HTTP calls and this runs on the trade path. The division of labour is
    "monitor decides what is admissible, this confirms nothing has changed since".
    """
    try:
        spec = _spec(code, issuer)
    except Exception:
        return None
    if _is_native(spec):
        return {'spec': spec, 'code': 'XLM', 'issuer': None}

    registry = _read_json(_VERIFIED_ASSETS_PATH, {})
    record = registry.get(spec)
    if not record:
        return None
    # A denial is permanent and outranks everything, including a later re-admission:
    # this is where a rugged or unsellable asset lands.
    if record.get('denied'):
        return None
    expires = record.get('expires_at')
    if expires and time.time() > expires:
        return None

    cached = _verify_cache.get(spec)
    if cached and time.time() - cached[0] < _VERIFY_TTL:
        return record if cached[1] else None

    ok = _still_tradeable(code, issuer)
    _verify_cache[spec] = (time.time(), ok)
    return record if ok else None


def _still_tradeable(code, issuer):
    """One cheap liveness check: does the asset still exist with safe flags?"""
    try:
        resp = requests.get(f'{_HORIZON}/assets',
                            params={'asset_code': code, 'asset_issuer': issuer},
                            timeout=_TIMEOUT)
        if resp.status_code != 200:
            return False
        records = resp.json().get('_embedded', {}).get('records', [])
        if not records:
            return False
        flags = records[0].get('flags') or {}
        # Clawback or a hard authorization requirement appearing after admission is
        # exactly the "was fine when admitted" case this check exists for.
        if flags.get('auth_clawback_enabled') or flags.get('auth_required'):
            return False
        return True
    except Exception:
        # Fail closed: an unreachable Horizon means we cannot confirm the asset is
        # still safe, and real money should not move on an unconfirmed asset.
        return False


def _source_address():
    result = subprocess.run(
        ["stellar", "keys", "address", _IDENTITY],
        capture_output=True, text=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"could not resolve {_IDENTITY}'s address")
    return result.stdout.strip()


def _account(address):
    resp = requests.get(f"{_HORIZON}/accounts/{address}", timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _asset_balance(account_json, code, issuer=None):
    """Real on-chain balance of an asset. None means no trustline (impossible for
    native XLM), which is a different fact from a real zero balance.

    `issuer` defaults to USDC's for backward compatibility with the original two-asset
    call sites, which passed only a code.
    """
    if code == "XLM":
        for b in account_json["balances"]:
            if b["asset_type"] == "native":
                return float(b["balance"])
        return 0.0
    if issuer is None:
        issuer = _USDC_ISSUER if code == _USDC_CODE else None
    for b in account_json["balances"]:
        if b.get("asset_code") == code and b.get("asset_issuer") == issuer:
            return float(b["balance"])
    return None


def _has_trustline(account_json, code, issuer):
    return _asset_balance(account_json, code, issuer) is not None


def _spendable_xlm(account_json):
    """XLM balance minus the account's minimum reserve and a small fee buffer, so a
    sell/wind_down chunk never tries to spend down to a balance the network will
    reject once its own transaction fee is deducted."""
    balance = _asset_balance(account_json, "XLM")
    reserve = (2.5 + account_json.get("subentry_count", 0)) * _BASE_RESERVE_XLM
    return max(0.0, balance - reserve - _FEE_BUFFER_XLM)


def _sellable_xlm(account_json):
    """Spendable XLM that wind_down() may liquidate — spendable minus the operating floor.

    Deliberately NOT used by submit_trade: a strategy selling its own position is meant
    to be able to exit it completely, and flooring that would silently change trading
    behaviour. This narrower notion applies only to the demotion path, where nothing is
    live and the leftover XLM is infrastructure rather than a position anyone chose.
    """
    return max(0.0, _spendable_xlm(account_json) - WIND_DOWN_RESERVE_XLM)


def _current_live_name():
    """Which strategy pubnet.log entries get attributed to.

    Read from monitor.py's own /opt/live_strategy.json rather than trusting a
    caller-supplied name: a (possibly revised) strategy shouldn't get to say who it is
    for audit purposes, any more than it gets to say how much it's allowed to spend.
    """
    try:
        return json.loads(_LIVE_STRATEGY_PATH.read_text())["name"]
    except Exception:
        return "unknown"


def _log_pubnet_trade(action, amount_usd, amount_xlm, tx_hash, reason=None, spec='XLM'):
    entry = {
        "timestamp": time.time(),
        "action": action,
        "amount_usd": amount_usd,
        "amount_xlm": amount_xlm,  # estimated from the pre-trade price, not the exact fill
        "tx_hash": tx_hash,
        "reason": reason,
        "asset": spec,
    }
    log_path = BASE_DIR / f"{_current_live_name()}.pubnet.log"
    with open(log_path, 'a') as f:
        f.write(json.dumps(entry) + "\n")


def _read_ledger():
    return _read_json(_LEDGER_PATH, [])


def _daily_spent(spec=None):
    """USD spent in the last 24h -- across all assets, or for one asset.

    Ledger entries written before multi-asset support have no `asset` field; they were
    all XLM, so they are treated as such rather than being dropped from the global total.
    """
    cutoff = time.time() - 86400
    total = 0.0
    for e in _read_ledger():
        if e.get("ts", 0) <= cutoff:
            continue
        if spec is not None and e.get("asset", "XLM") != spec:
            continue
        total += e.get("amount_usd", 0.0)
    return total


def _record_spend(amount_usd, spec='XLM'):
    cutoff = time.time() - 86400
    entries = [e for e in _read_ledger() if e.get("ts", 0) > cutoff]
    entries.append({"ts": time.time(), "amount_usd": amount_usd, "asset": spec})
    _write_json(_LEDGER_PATH, entries)


def _stuck_positions():
    return _read_json(_STUCK_PATH, {})


def _mark_stuck(spec, amount, usd_estimate, reason):
    """Record a leg that could not be sold, and escalate if the total is material.

    Three things fire together, deliberately. The asset is denied permanently so it can
    never be re-admitted; new non-XLM buys stop while any leg is stuck, since acquiring
    more illiquid exposure while holding an unsellable bag is exactly the wrong move;
    and past MAX_STUCK_USD the whole live path halts.
    """
    stuck = _stuck_positions()
    stuck[spec] = {'amount': amount, 'usd_estimate': usd_estimate,
                   'reason': reason, 'since': stuck.get(spec, {}).get('since', time.time())}
    _write_json(_STUCK_PATH, stuck)

    registry = _read_json(_VERIFIED_ASSETS_PATH, {})
    record = registry.get(spec, {})
    record.update({'denied': True, 'deny_reason': f'unsellable: {reason}',
                   'denied_at': time.time()})
    registry[spec] = record
    _write_json(_VERIFIED_ASSETS_PATH, registry)

    total = sum(v.get('usd_estimate', 0.0) for v in stuck.values())
    if total >= MAX_STUCK_USD and not _HALT_PATH.exists():
        _HALT_PATH.write_text(json.dumps({
            'halted_at': time.time(),
            'reason': f'${total:.2f} of unsellable positions exceeds '
                      f'${MAX_STUCK_USD:.2f}', 'stuck': stuck}, indent=2))
        print(f'[stellar_trader] LIVE HALT: ${total:.2f} stuck across '
              f'{len(stuck)} leg(s); delete {_HALT_PATH} to resume')


def _halted():
    return _HALT_PATH.exists()


def _to_stroops(amount):
    return max(0, round(amount * 10_000_000))


def ensure_trustline(code: str, issuer: str) -> dict:
    """Open a trustline for an admitted asset, if it is safe and affordable to do so.

    Returns {'ok', 'created', 'reason'}.

    Every trustline is a subentry costing 0.5 XLM of base reserve, permanently, and it
    raises the account's minimum balance -- which directly shrinks _spendable_xlm() and
    therefore the account's ability to sell XLM or pay fees. Opening one carelessly can
    strand the account below its own reserve, which is the failure mode claudio is in
    right now: 2.0099 XLM against a 2.0 protocol minimum, leaving 0.0 spendable.

    So this refuses unless the account would still clear MIN_XLM_OPERATING_BUFFER
    *after* the new subentry, and never opens more than MAX_SYSTEM_TRUSTLINES. Called by
    monitor at promotion time only -- never from a strategy.
    """
    if _paper_only():
        return {'ok': False, 'created': False, 'reason': 'PAPER_ONLY is set'}
    if _halted():
        return {'ok': False, 'created': False, 'reason': 'live trading is halted'}
    if not _verified_asset(code, issuer):
        return {'ok': False, 'created': False,
                'reason': f'{code} is not an admitted asset'}

    try:
        spec = _spec(code, issuer)
        address = _source_address()
        account = _account(address)
    except Exception as e:
        return {'ok': False, 'created': False, 'reason': str(e)}

    if _has_trustline(account, code, issuer):
        return {'ok': True, 'created': False, 'reason': 'trustline already exists'}

    ours = _read_json(_TRUSTLINES_PATH, {})
    if len(ours) >= MAX_SYSTEM_TRUSTLINES:
        return {'ok': False, 'created': False,
                'reason': f'already hold {len(ours)} trustlines '
                          f'(max {MAX_SYSTEM_TRUSTLINES})'}

    balance = _asset_balance(account, 'XLM')
    subentries = account.get('subentry_count', 0)
    after = balance - (2.5 + subentries + 1) * _BASE_RESERVE_XLM - _FEE_BUFFER_XLM
    if after < MIN_XLM_OPERATING_BUFFER:
        return {'ok': False, 'created': False,
                'reason': f'a new trustline would leave {after:.4f} XLM spendable, '
                          f'below the {MIN_XLM_OPERATING_BUFFER} buffer '
                          f'(balance {balance:.4f}, {subentries} subentries) -- '
                          f'fund the account first'}

    result = subprocess.run(
        ["stellar", "tx", "new", "change-trust",
         "--source", _IDENTITY, "--network", _NETWORK,
         "--line", f"{code}:{issuer}"],
        capture_output=True, text=True, timeout=_TX_TIMEOUT)
    if result.returncode != 0:
        return {'ok': False, 'created': False,
                'reason': result.stderr.strip() or 'change-trust failed'}

    ours[spec] = {'code': code, 'issuer': issuer, 'opened_at': time.time()}
    _write_json(_TRUSTLINES_PATH, ours)
    return {'ok': True, 'created': True, 'reason': None}


def remove_trustline(code: str, issuer: str) -> dict:
    """Close a trustline this system opened, refunding its 0.5 XLM reserve.

    Only closes trustlines recorded in _TRUSTLINES_PATH -- never one that predates this
    system (claudio's USDC and BLND lines are not ours to close). Refuses unless the
    balance is zero, since closing a funded trustline is rejected by the network anyway.

    The reserve refund is what makes asset rotation possible at claudio's size: without
    recycling, two trustlines permanently consume 1.0 XLM of a ~2 XLM account.
    """
    if _paper_only():
        return {'ok': False, 'removed': False, 'reason': 'PAPER_ONLY is set'}
    try:
        spec = _spec(code, issuer)
    except Exception as e:
        return {'ok': False, 'removed': False, 'reason': str(e)}

    ours = _read_json(_TRUSTLINES_PATH, {})
    if spec not in ours:
        return {'ok': False, 'removed': False,
                'reason': 'not a trustline this system opened'}
    try:
        account = _account(_source_address())
    except Exception as e:
        return {'ok': False, 'removed': False, 'reason': str(e)}

    balance = _asset_balance(account, code, issuer)
    if balance is None:
        ours.pop(spec, None)
        _write_json(_TRUSTLINES_PATH, ours)
        return {'ok': True, 'removed': False, 'reason': 'trustline already gone'}
    if balance > 0:
        return {'ok': False, 'removed': False,
                'reason': f'balance {balance} is non-zero; flatten the leg first'}

    result = subprocess.run(
        ["stellar", "tx", "new", "change-trust",
         "--source", _IDENTITY, "--network", _NETWORK,
         "--line", f"{code}:{issuer}", "--limit", "0"],
        capture_output=True, text=True, timeout=_TX_TIMEOUT)
    if result.returncode != 0:
        return {'ok': False, 'removed': False,
                'reason': result.stderr.strip() or 'change-trust --limit 0 failed'}

    ours.pop(spec, None)
    _write_json(_TRUSTLINES_PATH, ours)
    return {'ok': True, 'removed': True, 'reason': None}


def open_positions(ours_only=True):
    """Non-XLM legs held on chain, from Horizon.

    Read from the chain, never from any strategy's state.json: on-chain balances are the
    only truth about a real position, and a strategy's paper state can disagree with it
    for any number of reasons.

    `ours_only` (the default, and what wind_down uses) restricts the result to assets
    whose trustline THIS system opened, per _TRUSTLINES_PATH. That restriction is a
    safety property, not tidiness: claudio independently holds ~336 BLND that predates
    this system entirely and is described in the module docstring as irrelevant here.
    Without the filter, wind_down would enumerate BLND as a leg and try to market-sell
    someone else's position on the next leader change. This system liquidates only what
    it acquired.

    Pass ours_only=False for a full read-only inventory.
    """
    try:
        account = _account(_source_address())
    except Exception as e:
        print(f'[stellar_trader] could not read positions: {e}')
        return []
    ours = _read_json(_TRUSTLINES_PATH, {})
    out = []
    for b in account.get('balances', []):
        if b.get('asset_type') == 'native':
            continue
        code, issuer = b.get('asset_code'), b.get('asset_issuer')
        if code == _USDC_CODE and issuer == _USDC_ISSUER:
            continue      # settlement asset, not a position
        amount = float(b.get('balance', 0.0))
        if amount <= 0:
            continue
        try:
            spec = _spec(code, issuer)
        except Exception:
            continue
        if ours_only and spec not in ours:
            continue
        out.append({'spec': spec, 'code': code, 'issuer': issuer, 'amount': amount})
    return out


def _find_path(send_spec, send_amount, dest_spec):
    """Intermediate hops for a strict-send swap, or [] for a direct route.

    Necessary, not optional: many discovered assets have no direct order book against
    USDC. Checked live while building this -- USDC->AQUA routes through PYUSD. Asking
    Horizon per swap rather than caching, because routes change with liquidity, and a
    stale path that no longer fills is worse than a fresh lookup. dest_min still bounds
    slippage whatever route comes back.
    """
    import assets
    try:
        params = {'source_amount': f'{send_amount:.7f}',
                  'destination_assets': assets.sep11(dest_spec)}
        params.update(assets.horizon_params(send_spec, 'source_asset'))
        resp = requests.get(f'{_HORIZON}/paths/strict-send', params=params,
                            timeout=_TIMEOUT)
        if resp.status_code != 200:
            return []
        records = resp.json().get('_embedded', {}).get('records', [])
        if not records:
            return []
        best = max(records, key=lambda r: float(r.get('destination_amount', 0)))
        hops = []
        for hop in best.get('path', []):
            if hop.get('asset_type') == 'native':
                hops.append('native')
            else:
                hops.append(f"{hop['asset_code']}:{hop['asset_issuer']}")
        return hops
    except Exception:
        return []


def _swap(*, spend_spec, send_amount, dest_spec, dest_min):
    """Self-payment path-payment-strict-send: swap `send_amount` of `spend_spec` for at
    least `dest_min` of `dest_spec`.

    Both specs come from submit_trade/wind_down, never from a caller, and by the time
    they arrive the asset has passed _verified_asset and every cap.
    """
    address = _source_address()
    send_asset = _sep11(spend_spec)
    dest_asset = _sep11(dest_spec)
    argv = [
        "stellar", "tx", "new", "path-payment-strict-send",
        "--source", _IDENTITY,
        "--network", _NETWORK,
        "--send-asset", send_asset,
        "--send-amount", str(_to_stroops(send_amount)),
        "--destination", address,
        "--dest-asset", dest_asset,
        "--dest-min", str(_to_stroops(dest_min)),
    ]
    for hop in _find_path(spend_spec, send_amount, dest_spec):
        argv += ["--path", hop]

    result = subprocess.run(argv, capture_output=True, text=True, timeout=_TX_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "path-payment-strict-send failed")
    # Confirmed live (Rollout phase 1 REPL test, stellar CLI 27.0.0 in the trading
    # container): `stellar tx new` without --build-only does NOT reliably print the tx
    # hash to stdout/stderr in a regex-matchable form — a real submitted trade came
    # back with no hash in either stream. Rather than depend on CLI output formatting
    # (which already differs between the 25.2.0 on this host and 27.0.0 in the
    # container), cross-reference Horizon for the account's own most recent
    # transaction instead.
    combined = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"\b[0-9a-fA-F]{64}\b", combined)
    return match.group(0) if match else _most_recent_tx_hash(address)


def _most_recent_tx_hash(address):
    resp = requests.get(
        f"{_HORIZON}/accounts/{address}/transactions",
        params={"order": "desc", "limit": 1},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    records = resp.json()["_embedded"]["records"]
    return records[0]["hash"] if records else ""


def submit_trade(side: str, usd_amount: float, *, asset='XLM') -> dict:
    """Sign and submit a real XLM/USDC trade on pubnet using the `claudio` identity.

    side: 'buy' (spend USDC for XLM) or 'sell' (spend XLM for USDC). usd_amount:
    requested USD notional — silently clamped to MAX_TRADE_USD, the remaining
    MAX_DAILY_USD budget, and claudio's real on-chain balance of whatever asset the
    trade spends, regardless of what the caller asks for.

    Returns {'submitted': bool, 'tx_hash': str|None, 'amount_usd': float, 'reason': str|None}.
    """
    if _paper_only():
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": "PAPER_ONLY is set; refusing to submit a real trade"}

    if _halted():
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": f"live trading halted; see {_HALT_PATH}"}

    if side not in ("buy", "sell"):
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": f"invalid side {side!r}"}

    # `asset` is a request, not an instruction. It must already have been admitted by
    # monitor, and every cap below applies on top regardless of what was asked for.
    try:
        spec = _normalize_asset(asset)
    except Exception as e:
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": f"malformed asset {asset!r}: {e}"}

    native = _is_native(spec)
    code, issuer = (spec.split(':') + [None])[:2] if not native else ("XLM", None)

    if not native:
        if not _verified_asset(code, issuer):
            return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                    "reason": f"{spec} is not an admitted asset (or admission expired)"}
        # Holding something unsellable while buying more illiquid exposure is exactly
        # the wrong move, so any stuck leg blocks all non-XLM buys.
        if side == "buy" and _stuck_positions():
            return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                    "reason": "a previous leg is stuck; non-XLM buys are suspended"}

    price = get_price() if native else _asset_price(code, issuer)
    if price is None or price <= 0:
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": "no price available"}

    capped_usd = max(0.0, float(usd_amount))
    capped_usd = min(capped_usd, MAX_TRADE_USD, max(0.0, MAX_DAILY_USD - _daily_spent()))
    if not native:
        capped_usd = min(
            capped_usd,
            MAX_TRADE_USD_NONBASE,
            max(0.0, MAX_DAILY_USD_PER_ASSET - _daily_spent(spec)))

    account = _account(_source_address())

    if not native and side == "buy":
        held = _asset_balance(account, code, issuer)
        if held is None:
            return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                    "reason": f"no trustline for {spec}; call ensure_trustline first"}
        # Per-leg and aggregate exposure ceilings, both mark-to-market.
        capped_usd = min(capped_usd,
                         max(0.0, MAX_POSITION_USD_PER_ASSET - held * price))
        exposure = _total_nonbase_exposure(account)
        capped_usd = min(capped_usd,
                         max(0.0, MAX_TOTAL_NONBASE_EXPOSURE_USD - exposure))
        if len(open_positions()) >= MAX_OPEN_NONBASE_ASSETS and held <= 0:
            return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                    "reason": f"already holding {MAX_OPEN_NONBASE_ASSETS} non-XLM legs"}

    spend_spec = _USDC_SPEC if side == "buy" else spec
    if side == "sell":
        if native:
            available_usd = _spendable_xlm(account) * price
        else:
            held = _asset_balance(account, code, issuer)
            available_usd = (held or 0.0) * price
    else:
        usdc_balance = _asset_balance(account, _USDC_CODE, _USDC_ISSUER)
        if usdc_balance is None:
            return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                    "reason": "no USDC trustline"}
        available_usd = usdc_balance
    capped_usd = min(capped_usd, available_usd)

    if capped_usd <= 0:
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": "insufficient balance or caps exhausted"}

    dest_spec = spec if side == "buy" else _USDC_SPEC
    send_amount = capped_usd if side == "buy" else capped_usd / price
    dest_amount = capped_usd / price if side == "buy" else capped_usd
    dest_min = dest_amount * (1 - _SLIPPAGE)

    try:
        tx_hash = _swap(spend_spec=spend_spec, send_amount=send_amount,
                        dest_spec=dest_spec, dest_min=dest_min)
    except Exception as e:
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0, "reason": str(e)}

    _record_spend(capped_usd, spec)
    filled = dest_amount if side == "buy" else send_amount
    _log_pubnet_trade(side, capped_usd, filled if native else 0.0, tx_hash, spec=spec)
    return {"submitted": True, "tx_hash": tx_hash, "amount_usd": capped_usd,
            "reason": None}


def _asset_price(code, issuer):
    """USD mark for a non-XLM asset, via the shared DEX pricing module."""
    try:
        from dex_price import get_mark
        return get_mark(_spec(code, issuer))
    except Exception:
        return None


def _total_nonbase_exposure(account_json):
    """Mark-to-market USD across every non-XLM, non-USDC leg currently held."""
    total = 0.0
    for b in account_json.get('balances', []):
        if b.get('asset_type') == 'native':
            continue
        code, issuer = b.get('asset_code'), b.get('asset_issuer')
        if code == _USDC_CODE and issuer == _USDC_ISSUER:
            continue
        amount = float(b.get('balance', 0.0))
        if amount <= 0:
            continue
        mark = _asset_price(code, issuer)
        if mark:
            total += amount * mark
    return total


def wind_down() -> dict:
    """Liquidate claudio's real XLM position back to USDC, down to the operating floor.

    Called by monitor.py's promote_live_strategy when swapping the live strategy — not
    importable from template_repo/, same restriction as submit_trade. Exactly one
    strategy trades live at a time, so claudio's on-chain XLM balance at demotion time
    is attributable to the outgoing live strategy; there's no per-strategy position to
    track, just flatten what's on the account.

    Except that "what's on the account" is not all position. WIND_DOWN_RESERVE_XLM of
    spendable XLM is held back, because native XLM is also what pays fees and funds
    trustline reserves. Selling down to the protocol floor — which this function did
    until 2026-08-02 — leaves _spendable_xlm() at exactly 0.0, and from there every
    subsequent sell and every wind_down returns "insufficient balance": the next live
    strategy can open a position it cannot close, and the failure is invisible until a
    human reads the account. The floor is small in USD terms (~$0.35 at $0.175) and is
    the cheapest way to keep the sell leg alive across a leader change.

    Sells in MAX_TRADE_USD-sized chunks to avoid slippage from dumping a large position
    in one market sell, looping until the remaining balance is below the dust threshold
    or _MAX_WIND_DOWN_CHUNKS_PER_CALL is hit. NOT gated by MAX_DAILY_USD — that cap
    bounds loss from bad *buys*; applying it to a de-risking sell could trap claudio
    holding an unhedged position for days with no strategy live. MAX_TRADE_USD still
    applies per chunk, purely for slippage control.

    With extra assets, every non-XLM leg is flattened first and XLM last. That order is
    deliberate: the exotic legs are the illiquid ones and the ones most likely to become
    unsellable, so they should be exited while there is still time in the cycle, and XLM
    is both always sellable and the asset that pays the fees for the other sells.

    A leg that cannot be sold is marked stuck rather than blocking forever. `liquidated`
    still reports True once every non-stuck leg is flat, because the alternative -- a
    permanent False -- means a $0.40 bag of a dead token blocks every future leader
    change for good, which is strictly worse than eating the $0.40. _mark_stuck denies
    the asset permanently, suspends non-XLM buys, and halts everything past
    MAX_STUCK_USD, which is what makes that trade-off safe.

    Returns {'liquidated', 'remaining_xlm', 'chunks', 'reason', 'legs', 'stuck'}.
    monitor.promote_live_strategy gates on 'liquidated' and reports 'remaining_xlm';
    both now measure the *sellable* balance, i.e. net of WIND_DOWN_RESERVE_XLM. That is
    the load-bearing half of the floor: measuring 'liquidated' against raw spendable
    would leave it permanently False once the floor is reached, and a leader change
    would then never complete -- trading one stuck state for a worse one.
    """
    if _paper_only():
        return {"liquidated": False, "remaining_xlm": 0.0, "chunks": 0,
                "reason": "PAPER_ONLY is set; refusing to liquidate a real position",
                "legs": [], "stuck": []}

    chunks = 0
    legs = []

    # Non-XLM legs first, read from the chain rather than any strategy's state.json.
    for position in open_positions():
        spec, code, issuer = position['spec'], position['code'], position['issuer']
        sold_usd = 0.0
        stuck_reason = None
        for _ in range(_MAX_WIND_DOWN_CHUNKS_PER_CALL - chunks):
            account = _account(_source_address())
            held = _asset_balance(account, code, issuer) or 0.0
            mark = _asset_price(code, issuer)
            if mark is None or mark <= 0:
                stuck_reason = 'no price available'
                break
            if held * mark < 0.01:      # dust, in USD terms
                break
            chunk_usd = min(held * mark, MAX_TRADE_USD)
            send_amount = chunk_usd / mark
            dest_min = chunk_usd * (1 - _SLIPPAGE)
            try:
                tx_hash = _swap(spend_spec=spec, send_amount=send_amount,
                                dest_spec=_USDC_SPEC, dest_min=dest_min)
            except Exception as e:
                stuck_reason = str(e)
                break
            _log_pubnet_trade("wind_down_sell", chunk_usd, 0.0, tx_hash, spec=spec)
            sold_usd += chunk_usd
            chunks += 1

        account = _account(_source_address())
        remaining_leg = _asset_balance(account, code, issuer) or 0.0
        mark = _asset_price(code, issuer) or 0.0
        flat = remaining_leg * mark < 0.01
        if not flat and stuck_reason:
            _mark_stuck(spec, remaining_leg, remaining_leg * mark, stuck_reason)
        elif flat:
            remove_trustline(code, issuer)   # refunds 0.5 XLM of reserve
        legs.append({'spec': spec, 'sold_usd': round(sold_usd, 4),
                     'remaining': remaining_leg, 'flat': flat,
                     'stuck': bool(not flat and stuck_reason)})

    # XLM last.
    while chunks < _MAX_WIND_DOWN_CHUNKS_PER_CALL:
        account = _account(_source_address())
        remaining = _sellable_xlm(account)
        if remaining <= _XLM_DUST:
            break

        price = get_price()
        if price is None:
            return {"liquidated": False, "remaining_xlm": remaining, "chunks": chunks,
                    "reason": "no price available", "legs": legs,
                    "stuck": [l['spec'] for l in legs if l['stuck']]}

        chunk_usd = min(remaining * price, MAX_TRADE_USD)
        send_amount = chunk_usd / price
        dest_min = chunk_usd * (1 - _SLIPPAGE)
        try:
            tx_hash = _swap(spend_spec="XLM", send_amount=send_amount,
                            dest_spec=_USDC_SPEC, dest_min=dest_min)
        except Exception as e:
            return {"liquidated": False, "remaining_xlm": remaining, "chunks": chunks,
                    "reason": str(e), "legs": legs,
                    "stuck": [l['spec'] for l in legs if l['stuck']]}

        _log_pubnet_trade("wind_down_sell", chunk_usd, send_amount, tx_hash)
        chunks += 1

    remaining_xlm = _sellable_xlm(_account(_source_address()))
    stuck = [l['spec'] for l in legs if l['stuck']]
    if remaining_xlm > _XLM_DUST:
        return {"liquidated": False, "remaining_xlm": remaining_xlm, "chunks": chunks,
                "reason": "chunk limit reached this cycle, will retry",
                "legs": legs, "stuck": stuck}
    return {"liquidated": True, "remaining_xlm": remaining_xlm, "chunks": chunks,
            "reason": f'{len(stuck)} leg(s) stuck' if stuck else None,
            "legs": legs, "stuck": stuck}


if __name__ == "__main__":
    address = _source_address()
    account = _account(address)
    xlm = _asset_balance(account, "XLM")
    usdc = _asset_balance(account, "USDC")
    print(f"claudio ({address})")
    print(f"  XLM:  {xlm:.7f} (spendable above reserve: {_spendable_xlm(account):.7f}, "
          f"wind_down-sellable: {_sellable_xlm(account):.7f})")
    print(f"  USDC: {'no trustline' if usdc is None else f'{usdc:.7f}'}")
    print(f"  daily spend used: ${_daily_spent():.2f} / ${MAX_DAILY_USD:.2f}")
    print("(read-only status check — submit_trade()/wind_down() are not called here)")

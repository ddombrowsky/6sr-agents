#!/usr/bin/env python3
"""Real-money trading on Stellar pubnet using the `claudio` identity.

XLM plus any asset monitor has admitted; USDC is the settlement asset every trade is
denominated against, not something a strategy selects. Non-XLM real trading was enabled
on 2026-08-03 — before that, trade_logger refused every non-XLM submission and this
module's asset support was reachable only from a REPL.

This module is the actual safety boundary that plan describes: every cap below lives only
here, is never expressed as a caller-supplied parameter, and is enforced regardless of
what a (possibly LLM-revised) strategy asks for.

Shells out to the `stellar` CLI for signing/submission, same shell-out pattern as
reflector_oracle.py, and to Horizon's REST API for balance queries — classic XLM/USDC
balances live in account trustlines, not a Soroban contract, so `contract invoke` (what
reflector_oracle.py uses) doesn't apply here.

Confirmed live against `claudio`
(GBTFQJ6VARJYI2C6JLPUXQ4CAKRNJF3KEYXXJ5T74DV47RSSNIJCH5VM), via a plain Horizon GET, not
assumed:
- already holds a USDC trustline - issuer GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN,
  the well-known Circle-issued USDC on pubnet.
- as of 2026-08-03, ~12.26 XLM and ~61.4 USDC, with 2 subentries (also holds some BLND,
  which predates this system and is irrelevant here). Funded from ~2.26 XLM that day to
  buy reserve headroom for discovered-asset trustlines. Run this file directly for the
  current numbers rather than trusting this line; it has been stale before.

Trading itself uses a self-payment `path-payment-strict-send` (source == destination ==
claudio's own address) - the standard way to take a market-order-like swap off the
Stellar DEX orderbook from the CLI, as opposed to manage-buy-offer/manage-sell-offer,
which post a *resting* limit order that may not fill immediately or in full.

CAUTION: submit_trade()/wind_down()/ensure_trustline()/remove_trustline() sign and submit
real pubnet transactions. They are wired into trade_logger and monitor.py; exercise any
CHANGE to them manually from a REPL first, at these cap sizes. Running this file directly
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
MAX_DAILY_USD = 2000.0                 # per 24h across ALL assets combined; see below
MAX_TRADE_USD_NONBASE = 0.50           # per trade, non-XLM only
MAX_DAILY_USD_PER_ASSET = 4.0          # per 24h per (code, issuer)
MAX_POSITION_USD_PER_ASSET = 4.0       # mark-to-market cap on any one non-XLM leg
MAX_TOTAL_NONBASE_EXPOSURE_USD = 8.0   # across every non-XLM leg at once
MAX_OPEN_NONBASE_ASSETS = 2            # matches the strategy-side limit
MAX_SYSTEM_TRUSTLINES = 3              # USDC + 2 discovered. BLND predates this system.
MIN_XLM_OPERATING_BUFFER = 1.0         # spendable XLM that must survive a new trustline
MAX_STUCK_USD = 2.0                    # total unsellable notional before a full halt
_VERIFY_TTL = 900                      # re-verification cache in the hot path

# MAX_DAILY_USD was 99999.0 from `72fc3f4 TEMP: max-daily isn't working` until 2026-08-18,
# and MAKER.md's risk list asks for it to be a real number before phase 4: a maker books
# far more fills per day than a taker, so this cap goes from dormant to load-bearing.
#
# Sized from the 48h paper run, not chosen for roundness. Each seed's BUY volume, scaled
# from the paper quote size it actually ran to the live MAX_RESTING_USD_PER_SIDE of $4:
#
#   seed_maker04  $442/day      seed_maker02  $656/day
#   seed_maker03  $543/day      seed_maker05  $748/day
#   seed_maker00  $590/day      seed_maker01  $949/day
#
# So a live maker under the caps in this file transacts roughly $450-$950 of buys a day,
# and 2000.0 is a little over twice the busiest of them. That is the intent: this is not
# a position limit and must not be set as if it were one. Exposure is already bounded by
# MAX_RESTING_USD_TOTAL ($8) and MAX_INVENTORY_SKEW_USD ($8), and a maker's buys are
# matched by sells, so gross daily buy volume is turnover, not money at risk. What this
# cap is for is the runaway: a requote loop that crosses the spread, or a fill-detection
# bug that books the same bid over and over. Those hit $2000 in hours, and the bid stops.
#
# Consequence of picking a number at all: place_offer sizes a bid against the REMAINING
# budget, so once the day's buys reach the cap the bid goes to zero and the strategy
# quotes one-sided until the trailing 24h window rolls. That is a real behaviour change
# from 99999.0, and it is the intended one -- but it means a cap set too tight looks like
# a strategy that mysteriously stopped bidding, not like an alarm. Check
# daily_spend_status() before concluding a quoter is broken.

# --- maker caps (MAKER.md phase 2) -------------------------------------------------
# Same rules as the block above: module-level, never caller-supplied, never readable from
# a config.json, only changeable by a human edit that check_boundary_integrity will halt
# over. They are a SEPARATE set from the caps above and not a rename of them, because a
# resting offer and a fill are different kinds of risk:
#
#   a resting offer is an EXPOSURE question -- how much am I promising the market right
#   now, and how much of the account is encumbered by that promise
#   a fill is a SPEND question -- how much money actually moved
#
# The per-trade and daily caps above still apply to whatever fills. These bound what may
# be resting before anything fills at all, which the existing caps cannot express.
MAX_OPEN_OFFERS = 4                    # subentry reserve: 0.5 XLM each, see below
MAX_RESTING_USD_PER_SIDE = 4.0         # matches MAX_TRADE_USD; one quote, one cap
MAX_RESTING_USD_TOTAL = 8.0            # both sides, every offer, at once
MAX_INVENTORY_SKEW_USD = 8.0           # |long - short| before quotes go one-sided
MIN_QUOTE_WIDTH_BP = 2.0               # never quote inside the fee/rounding floor

# A safety cap, NOT a strategy knob, and the distinction is the whole reason it lives
# here. A maker process that dies with quotes resting is the one failure mode this system
# has never had: a path-payment either completes or does not, but an abandoned offer keeps
# trading on behalf of a strategy that no longer exists, and loses money while nothing at
# all is running. Bounding it must not be something the agent can widen.
MAX_OFFER_AGE_S = 900

# Denominator bound when converting a decimal price to the rational Stellar wants. Large
# enough that the rounding is far inside MIN_QUOTE_WIDTH_BP, small enough that the n:d the
# CLI receives stays legible in a log line.
_PRICE_DENOMINATOR = 10_000_000

# Target sellable-XLM headroom (in USD, above MIN_TRUSTLINE_RESERVE_XLM) a freshly
# promoted leader should have before its own trading starts. See
# ensure_trading_cushion's docstring for the incident this exists to prevent.
MIN_LIVE_TRADING_CUSHION_USD = 5 * MAX_TRADE_USD
_MAX_CUSHION_CHUNKS = 5                # bounds one promotion's worth of top-up buys

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
# Spendable XLM that the trading path will not sell. Not a safety cap like the ones at the
# top of this file -- it is infrastructure. Every non-XLM leg needs a trustline, every
# trustline costs 0.5 XLM of base reserve, and every transaction costs a fee, all paid out
# of the same native balance a sell would otherwise spend to the floor. Sized so the
# account can open every trustline it is allowed to and keep the operating buffer on top.
#
# Renamed from WIND_DOWN_RESERVE_XLM on 2026-08-03 when it stopped being wind_down-only;
# see _sellable_xlm's docstring for why the ordinary sell path now honours it too.
#
# MAX_OPEN_OFFERS joins the same arithmetic, and must: an offer is a subentry exactly as a
# trustline is, costing the same 0.5 XLM of base reserve and raising the account's minimum
# balance the same way. Leave it out and a maker with four quotes open pushes the account
# under its own reserve, at which point EVERY operation fails -- not just the next offer,
# but the sell that would have unwound the position and the fee on the transaction that
# would have cancelled the offers. The cost of including it is 2.0 XLM (~$0.32) of
# headroom that ordinary trading may not spend, which is the correct trade.
MIN_TRUSTLINE_RESERVE_XLM = (MIN_XLM_OPERATING_BUFFER
                             + MAX_OPEN_NONBASE_ASSETS * _BASE_RESERVE_XLM
                             + MAX_OPEN_OFFERS * _BASE_RESERVE_XLM)

# Real, pre-funded XLM set aside to back short-sells (SHORTING_PLAN.md). Not part of any
# strategy's ordinary trading capital: _sellable_xlm floors above it whenever a buffer has
# been funded, so an ordinary sell or wind_down can never spend it by accident. There is
# no protocol margin here -- a short-sell literally spends out of this pre-funded reserve,
# and a cover buy replenishes it.
SHORT_BUFFER_XLM = 60.0


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
# Written once, by hand, after an operator deposits SHORT_BUFFER_XLM beyond the ordinary
# reserve. submit_trade re-checks this independently of a caller's short=True -- a
# short-sell request with no recorded, sufficiently-funded buffer is refused, never
# silently allowed through. See _short_buffer_funded.
_SHORT_BUFFER_PATH = BASE_DIR / '.short_buffer.json'

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


def _selling_liabilities(account_json, code, issuer=None):
    """Balance of `code` already committed to resting offers, in units of that asset.

    Stellar does NOT deduct a resting sell offer from `balance`; it raises
    `selling_liabilities` on the same balance entry and enforces
    `balance - selling_liabilities >= reserve` at submission time. So an account with
    320 XLM and a 100 XLM sell offer open still reports 320, and every caller that sizes
    against `balance` is sizing against XLM it has already promised to somebody else.

    This was harmless for as long as this system only ever took liquidity -- a
    path-payment either completes or does not, and leaves no liability behind -- and both
    fields read 0.0000000 on claudio for that whole period. The moment place_offer exists
    it stops being harmless, and it stops being harmless for the TAKER paths too: an
    ordinary sell sized against phantom balance fails on submission with Underfunded, and
    wind_down's "liquidated" verdict is computed from the same number.
    """
    if code == "XLM":
        for b in account_json["balances"]:
            if b["asset_type"] == "native":
                return float(b.get("selling_liabilities") or 0.0)
        return 0.0
    if issuer is None:
        issuer = _USDC_ISSUER if code == _USDC_CODE else None
    for b in account_json["balances"]:
        if b.get("asset_code") == code and b.get("asset_issuer") == issuer:
            return float(b.get("selling_liabilities") or 0.0)
    return 0.0


def _free_balance(account_json, code, issuer=None):
    """Balance of a non-native asset that is not already committed to a resting offer.

    The counterpart to _spendable_xlm for the settlement asset: a resting BID commits
    USDC exactly as a resting ask commits XLM. None (no trustline) passes through
    unchanged, because "no trustline" is a different fact from "zero free balance" and
    submit_trade distinguishes them.
    """
    held = _asset_balance(account_json, code, issuer)
    if held is None:
        return None
    return max(0.0, held - _selling_liabilities(account_json, code, issuer))


def _spendable_xlm(account_json):
    """XLM balance minus resting-offer liabilities, the account's minimum reserve, and a
    small fee buffer, so a sell/wind_down chunk never tries to spend down to a balance the
    network will reject once its own transaction fee is deducted.

    `selling_liabilities` is subtracted FIRST and unconditionally -- see
    _selling_liabilities for why a resting offer is invisible in `balance`. Every caller
    below this (_sellable_xlm, _short_sellable_xlm, ensure_trustline's headroom check,
    ensure_trading_cushion, wind_down) inherits the correction from here rather than
    repeating it, which is deliberate: this is the one place the number is defined.
    """
    balance = _asset_balance(account_json, "XLM")
    committed = _selling_liabilities(account_json, "XLM")
    reserve = (2.5 + account_json.get("subentry_count", 0)) * _BASE_RESERVE_XLM
    return max(0.0, balance - committed - reserve - _FEE_BUFFER_XLM)


def _short_buffer_funded():
    """True only if a human has recorded a real, sufficient SHORT_BUFFER_XLM deposit.

    Fails closed on a missing file, a malformed one, or a recorded amount below
    SHORT_BUFFER_XLM -- a short-sell request is refused rather than silently allowed
    through on the caller's say-so alone, the same "request, not instruction" pattern
    _verified_asset already applies to asset admission.
    """
    record = _read_json(_SHORT_BUFFER_PATH, None)
    if not record:
        return False
    try:
        return float(record.get('funded_xlm', 0.0)) >= SHORT_BUFFER_XLM
    except (TypeError, ValueError):
        return False


def _sellable_xlm(account_json):
    """Spendable XLM that may be sold — spendable minus the trustline/fee reserve.

    Used by BOTH wind_down and submit_trade's native sell leg, which is a reversal. This
    was previously wind_down-only, on the argument that "a strategy selling its own
    position is meant to be able to exit it completely, and flooring that would silently
    change trading behaviour".

    That argument assumed the strategy's position and the real balance were the same
    thing. They are not: the paper book runs at ~$1000 while claudio holds ~$5, so a
    strategy in a sell regime does not exit a position, it issues sells until the real
    account hits its protocol floor. Observed 2026-08-03 -- five ordinary sells in 13
    minutes (.pubnet_ledger.json ts 1785778828..1785779615), the last one a partial
    $1.4364 because `_spendable_xlm(account) * price` was all that was left. The account
    ended at 0.0000 spendable, which is precisely the state in which no trustline can be
    opened, so wind_down would carefully preserve this reserve at a leader change and the
    next strategy's ordinary sells would spend it straight back down.

    What the floor actually costs a strategy: MIN_TRUSTLINE_RESERVE_XLM is 2.0 XLM, about
    $0.35, against a MAX_TRADE_USD chunk of $4.00 ≈ 23 XLM. Under 9% of a single chunk,
    and only ever on the last sell of a full liquidation.

    When a short buffer has been funded (SHORTING_PLAN.md), the floor rises by
    SHORT_BUFFER_XLM on top of MIN_TRUSTLINE_RESERVE_XLM, so an ordinary sell or
    wind_down -- both call this, never _short_sellable_xlm -- can never spend into it by
    accident.
    """
    floor = MIN_TRUSTLINE_RESERVE_XLM
    if _short_buffer_funded():
        floor += SHORT_BUFFER_XLM
    return max(0.0, _spendable_xlm(account_json) - floor)


def _short_sellable_xlm(account_json):
    """The short buffer itself — spendable XLM above only MIN_TRUSTLINE_RESERVE_XLM.

    Used exclusively by submit_trade's short-sell path, and only once
    _short_buffer_funded() has independently confirmed the buffer is real. Deliberately
    does NOT also subtract SHORT_BUFFER_XLM -- that would floor a short-sell out of the
    very reserve it exists to draw down.
    """
    return max(0.0, _spendable_xlm(account_json) - MIN_TRUSTLINE_RESERVE_XLM)


def short_buffer_status():
    """Public status check for the short buffer (SHORTING_PLAN.md): is it funded, and
    does claudio's live spendable XLM right now actually cover the required floor?

    The public counterpart to the _short_buffer_funded/_short_sellable_xlm internals
    above, for callers outside this module -- domain_sdex.can_execute_live gates
    promotion of an allow_shorting strategy on this, since a strategy's config flag
    saying "shorting is on" does not imply the real money backing it exists. Fails
    closed (funded=False) on any read/network error.

    Returns {'funded': bool, 'spendable_xlm': float|None, 'required_xlm': float,
             'reason': str|None}.
    """
    required = MIN_TRUSTLINE_RESERVE_XLM + SHORT_BUFFER_XLM
    if not _short_buffer_funded():
        return {'funded': False, 'spendable_xlm': None, 'required_xlm': required,
                'reason': f'no funded buffer recorded at {_SHORT_BUFFER_PATH}'}
    try:
        account = _account(_source_address())
        spendable = _spendable_xlm(account)
    except Exception as e:
        return {'funded': False, 'spendable_xlm': None, 'required_xlm': required,
                'reason': f'could not read live XLM balance: {e}'}
    if spendable < required:
        return {'funded': False, 'spendable_xlm': spendable, 'required_xlm': required,
                'reason': f'spendable {spendable:.4f} XLM below required {required} XLM'}
    return {'funded': True, 'spendable_xlm': spendable, 'required_xlm': required,
            'reason': None}


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
    strand the account below its own reserve. claudio has been in that state twice:
    2.0099 XLM against a 2.0 protocol minimum (2026-08-02), and again at 2.2600 with 2
    subentries on 2026-08-03, both times 0.0 spendable.

    So this refuses unless the account would still clear MIN_XLM_OPERATING_BUFFER
    *after* the new subentry, and never opens more than MAX_SYSTEM_TRUSTLINES. Called by
    monitor.promote_live_strategy, once per declared asset, immediately after the
    outgoing strategy's wind_down and before the incoming one goes live -- never from a
    strategy. A refusal here is not fatal: the strategy goes live XLM-only and the
    reason is recorded in live_strategy.json.
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


def submit_trade(side: str, usd_amount: float, *, asset='XLM', short=False) -> dict:
    """Sign and submit a real trade on pubnet using the `claudio` identity.

    side: 'buy' (spend USDC for `asset`) or 'sell' (spend `asset` for USDC). asset:
    'XLM', 'CODE:ISSUER', or a {'code','issuer'} dict — a *request*, not an instruction:
    a non-XLM asset must already have been admitted by monitor and have a trustline, and
    every cap applies on top regardless. usd_amount: requested USD notional — silently
    clamped to MAX_TRADE_USD, the remaining MAX_DAILY_USD budget, and claudio's real
    on-chain balance of whatever asset the trade spends.

    Non-XLM buys are clamped further, by MAX_TRADE_USD_NONBASE, the remaining
    MAX_DAILY_USD_PER_ASSET budget, MAX_POSITION_USD_PER_ASSET and
    MAX_TOTAL_NONBASE_EXPOSURE_USD. Sells are not: those cap entry risk, and applying
    them to an exit is what made a fully-sized leg unsellable for 24h.

    short: XLM-only (SHORTING_PLAN.md). A *request* like `asset`, not an instruction --
    honoured only for a native sell, and only once _short_buffer_funded() has
    independently confirmed a real, sufficient deposit is recorded at
    _SHORT_BUFFER_PATH. When honoured, the sell draws against the pre-funded short
    buffer (_short_sellable_xlm) instead of ordinary trading capital (_sellable_xlm), so
    it can spend the account down past the ordinary MIN_TRUSTLINE_RESERVE_XLM floor.
    Ignored on every other side/asset combination.

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

    # short is a request, not an instruction (see docstring): re-verified here rather
    # than trusted, so a short-sell request with no recorded, sufficiently-funded buffer
    # is refused outright instead of silently falling through to an ordinary sell that
    # could dip into infrastructure reserve.
    if short and side == "sell" and native and not _short_buffer_funded():
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": f"short-sell requested but no funded buffer recorded at "
                          f"{_SHORT_BUFFER_PATH}"}

    if not native:
        # Admission is absolute for buys -- this is the third and only unskippable
        # verification gate, and nothing may move money *into* an unadmitted asset.
        #
        # It is not absolute for sells. Applied to both sides it made an asset that was
        # denied, expired, or merely unreachable impossible to exit through the ordinary
        # path, leaving only wind_down at the next leader change. Worse, _still_tradeable
        # fails closed and caches for _VERIFY_TTL, so one Horizon hiccup locked the sell
        # leg for 15 minutes on a position already held. Refusing to let money *out* of
        # something the system is already holding is not a safety property; it is the
        # same class of error as charging exits against the daily buy budget.
        #
        # The exemption is deliberately narrow: only a position this system actually
        # opened a trustline for, and actually holds a non-zero balance of. It cannot be
        # used to reach a new asset, because you cannot sell what you do not hold.
        if not _verified_asset(code, issuer):
            exempt = False
            if side == "sell":
                try:
                    account = _account(_source_address())
                    exempt = (spec in _read_json(_TRUSTLINES_PATH, {})
                              and (_asset_balance(account, code, issuer) or 0.0) > 0)
                except Exception:
                    exempt = False
            if not exempt:
                return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                        "reason": f"{spec} is not an admitted asset (or admission expired)"}
            print(f"[stellar_trader] {spec} is no longer admitted; allowing the sell of a "
                  f"position already held")
        # Holding something unsellable while buying more illiquid exposure is exactly
        # the wrong move, so any stuck leg blocks all non-XLM buys.
        if side == "buy" and _stuck_positions():
            return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                    "reason": "a previous leg is stuck; non-XLM buys are suspended"}

    price = get_price() if native else _asset_price(code, issuer)
    if price is None or price <= 0:
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": "no price available"}

    capped_usd = max(0.0, float(usd_amount)/10.0)
    capped_usd = min(capped_usd, MAX_TRADE_USD, max(0.0, MAX_DAILY_USD - _daily_spent()))
    if not native and side == "buy":
        # Buy-side only, which wind_down's docstring has argued for all along: "that cap
        # bounds loss from bad *buys*; applying it to a de-risking sell could trap claudio
        # holding an unhedged position for days". That reasoning was never carried across
        # to the non-base path, and the result was a lock rather than a cap: a leg that
        # reached MAX_POSITION_USD_PER_ASSET ($4) by buying had also spent the whole
        # MAX_DAILY_USD_PER_ASSET ($4) budget, so its own sells clamped to
        # min(..., max(0.0, 4.0 - 4.0)) == 0 and it was unsellable for 24h by
        # construction -- reported as "insufficient balance or caps exhausted".
        #
        # A sell stays bounded by MAX_TRADE_USD ($4), the same per-chunk bound wind_down
        # uses, which is slippage control rather than a risk cap. That is comfortable
        # against the real books of the assets currently admitted: AQUA 1.48% spread on
        # $24,239 of bid depth, ARS 1.95% on $970 (.verified_assets.json, 2026-08-03).
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
    floored_reason = None
    if side == "sell":
        if native:
            if short:
                # Fail-closed funding check already ran above; this draws against the
                # buffer itself rather than ordinary trading capital.
                available_usd = _short_sellable_xlm(account) * price
            else:
                # _sellable_xlm, not _spendable_xlm: the trustline and fee reserve is
                # infrastructure, not part of any strategy's position. See its
                # docstring. Also floors above any funded short buffer, so an ordinary
                # sell can never spend into it.
                available_usd = _sellable_xlm(account) * price
            if available_usd <= 0 < _spendable_xlm(account):
                # Distinct from an empty account, which is what this used to look like.
                # "insufficient balance or caps exhausted" was 101 of 133 live records on
                # 2026-08-03 and told an operator nothing about which of the two it was.
                floored_reason = (
                    f'native sell floored at MIN_TRUSTLINE_RESERVE_XLM '
                    f'({MIN_TRUSTLINE_RESERVE_XLM} XLM held back for trustlines and fees)')
        else:
            held = _asset_balance(account, code, issuer)
            available_usd = (held or 0.0) * price
    else:
        # _free_balance, not _asset_balance: a resting BID commits USDC via
        # selling_liabilities exactly as a resting ask commits XLM, and a buy sized
        # against the gross balance would be spending the same dollars twice.
        usdc_balance = _free_balance(account, _USDC_CODE, _USDC_ISSUER)
        if usdc_balance is None:
            return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                    "reason": "no USDC trustline"}
        available_usd = usdc_balance
    capped_usd = min(capped_usd, available_usd)

    if capped_usd <= 0:
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": floored_reason or "insufficient balance or caps exhausted"}

    dest_spec = spec if side == "buy" else _USDC_SPEC
    send_amount = capped_usd if side == "buy" else capped_usd / price
    dest_amount = capped_usd / price if side == "buy" else capped_usd
    dest_min = dest_amount * (1 - _SLIPPAGE)

    try:
        tx_hash = _swap(spend_spec=spend_spec, send_amount=send_amount,
                        dest_spec=dest_spec, dest_min=dest_min)
    except Exception as e:
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0, "reason": str(e)}

    # Buys only. A sell is not a spend, and counting it as one meant an exit consumed the
    # very budget the next entry needed -- and, with the buy-side clamp above now the only
    # reader of _daily_spent(spec), would have left that counter measuring something it no
    # longer gates. MAX_DAILY_USD is unaffected in practice today (72fc3f4 set it to
    # 99999.0), but it is the same error and would surface the moment that is fixed.
    if side == "buy":
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
    """Mark-to-market USD across the non-XLM legs THIS SYSTEM opened, per _TRUSTLINES_PATH.

    The `ours` filter is the same one open_positions applies, and for the same reason:
    claudio independently holds a BLND position that predates this system entirely, which
    open_positions excludes, wind_down never sells and remove_trustline refuses to close.

    Counting it here was a live bug, found by the first real non-XLM trade this system
    ever attempted (2026-08-03). BLND marked at $10.34 against a
    MAX_TOTAL_NONBASE_EXPOSURE_USD of $8.00, so headroom was $0.00 and EVERY non-XLM buy
    was refused -- permanently, and reported as the generic "insufficient balance or caps
    exhausted". The cap bounds risk this system chooses to take on; a position it can
    neither manage nor exit is not that, and letting a third party's balance move the
    limit means an asset nobody here controls decides whether this system may trade.
    """
    ours = _read_json(_TRUSTLINES_PATH, {})
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
        try:
            if _spec(code, issuer) not in ours:
                continue
        except Exception:
            continue
        mark = _asset_price(code, issuer)
        if mark:
            total += amount * mark
    return total


def ensure_trading_cushion(target_usd=None) -> dict:
    """Buy XLM with USDC, in MAX_TRADE_USD chunks, until sellable headroom above
    MIN_TRUSTLINE_RESERVE_XLM clears target_usd (default MIN_LIVE_TRADING_CUSHION_USD),
    or funds/chunks run out.

    Called once at promotion, by domain_sdex.ensure_trading_cushion, AFTER live.flag and
    live_strategy.json already point at the new leader -- so each chunk goes through the
    ordinary submit_trade path and attributes correctly via _current_live_name(), the
    same audit trail every other real trade goes through. Do not call this before
    live_strategy.json is updated, or the buys would misattribute to whoever was live
    before.

    Why this exists: a freshly-promoted strategy inherits whatever real balance the
    outgoing leader's wind_down left behind (bounded below only by
    MIN_TRUSTLINE_RESERVE_XLM), which can be thin. If it then opens with several
    same-direction ticks in a row -- ordinary behavior for any threshold strategy whose
    price sits past a threshold for a few minutes -- it grinds straight into the reserve
    floor on its first sells rather than its last. Observed on clone_72b9b4cd5752,
    2026-08-10: two clean $4 sells, a $0.02 partial, then eight refusals in four minutes,
    while the paper book kept crediting full-sized sells throughout -- the single largest
    driver of that promotion's paper/live return gap. A small cushion bought up front
    gives the first real burst of trading room to work with instead.

    submit_trade's usd_amount is a paper-scale request that it divides by 10 before
    capping (real trades run at 1/10th paper size); passing 10 * MAX_TRADE_USD is what
    makes each chunk land at exactly MAX_TRADE_USD once capped.

    Never raises and never blocks promotion, same contract as prepare_live: an account
    that stays thin just means more early floor refusals, not a broken promotion.

    Returns {'topped_up_usd': float, 'chunks': int, 'reason': str | None}.
    """
    if target_usd is None:
        target_usd = MIN_LIVE_TRADING_CUSHION_USD

    if _paper_only():
        return {'topped_up_usd': 0.0, 'chunks': 0,
                'reason': 'PAPER_ONLY is set; refusing to fund a real cushion'}
    if _halted():
        return {'topped_up_usd': 0.0, 'chunks': 0,
                'reason': f'live trading halted; see {_HALT_PATH}'}

    spent = 0.0
    chunks = 0
    for _ in range(_MAX_CUSHION_CHUNKS):
        price = get_price()
        if price is None or price <= 0:
            return {'topped_up_usd': spent, 'chunks': chunks,
                    'reason': 'no price available'}
        account = _account(_source_address())
        have_usd = _sellable_xlm(account) * price
        if have_usd >= target_usd:
            break
        result = submit_trade('buy', 10 * MAX_TRADE_USD, asset='XLM')
        if not result.get('submitted'):
            return {'topped_up_usd': spent, 'chunks': chunks,
                    'reason': result.get('reason')}
        spent += result.get('amount_usd', 0.0)
        chunks += 1
    return {'topped_up_usd': spent, 'chunks': chunks, 'reason': None}


def wind_down() -> dict:
    """Liquidate claudio's real XLM position back to USDC, down to the operating floor.

    Called by monitor.py's promote_live_strategy when swapping the live strategy — not
    importable from template_repo/, same restriction as submit_trade. Exactly one
    strategy trades live at a time, so claudio's on-chain XLM balance at demotion time
    is attributable to the outgoing live strategy; there's no per-strategy position to
    track, just flatten what's on the account.

    Except that "what's on the account" is not all position. MIN_TRUSTLINE_RESERVE_XLM of
    spendable XLM is held back, because native XLM is also what pays fees and funds
    trustline reserves. Selling down to the protocol floor — which this function did
    until 2026-08-02 — leaves _spendable_xlm() at exactly 0.0, and from there every
    subsequent sell and every wind_down returns "insufficient balance": the next live
    strategy can open a position it cannot close, and the failure is invisible until a
    human reads the account. The floor is small in USD terms (~$0.35 at $0.175) and is
    the cheapest way to keep the sell leg alive across a leader change.

    Since 2026-08-03 submit_trade's native sell honours the same floor, so this is no
    longer the only place it is preserved -- it was being restored here at each leader
    change and spent back down within the hour by ordinary trading.

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
    both now measure the *sellable* balance, i.e. net of MIN_TRUSTLINE_RESERVE_XLM. That is
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


# =====================================================================================
# Offer lifecycle -- MAKER.md phase 2
# =====================================================================================
#
# Everything above takes liquidity. Everything below POSTS it, and the difference is not
# a parameter: a path-payment resolves inside one transaction and leaves nothing behind,
# while an offer keeps standing in the market after the process that placed it has gone.
# That is a failure mode this system has never had, so three things exist purely to bound
# it, and all three sit here rather than in a strategy or a domain: MAX_OFFER_AGE_S,
# reconcile_offers(), and cancel_all_offers().
#
# The requote primitive is `--offer-id` on an existing offer, which Stellar treats as an
# atomic cancel/replace. There is deliberately no place_then_cancel path: a two-transaction
# requote leaves a window in which the strategy is either unquoted or double-quoted, and
# the double-quoted half of that window is real money resting at a stale price.

def _price_rational(price):
    """A decimal price as the (n, d) Stellar wants, with a bounded denominator.

    Returned rather than formatted so the caller can log the rational ACTUALLY submitted:
    the offer rests at n/d, not at the float that was asked for, and the difference --
    tiny, but real -- is the sort of thing that turns up later as an unexplained basis
    point. At _PRICE_DENOMINATOR the error on a $0.158 price is under 0.00007 bp, three
    orders of magnitude inside MIN_QUOTE_WIDTH_BP.
    """
    if not price or price <= 0:
        raise ValueError(f'price must be positive, got {price!r}')
    d = _PRICE_DENOMINATOR
    n = max(1, round(price * d))
    return n, d


def open_offers(ours_only=True):
    """Every offer claudio currently has resting, straight from Horizon.

    ON-CHAIN TRUTH, never a local ledger. The local view of what is resting can be wrong
    in both directions -- a submission that timed out may have landed, and an offer we
    think is resting may have filled -- and every safety decision below is made against
    this, not against what we believe.

    `ours_only` filters to the XLM/USDC pair this system trades; anything else on the
    account was not put there by this code and must not be cancelled by it.

    Returns [{'id', 'side', 'price', 'amount_xlm', 'usd', 'selling', 'buying'}] with
    `side` normalised to the maker's own vocabulary: 'ask' when we are selling XLM,
    'bid' when we are buying it. Returns [] on any failure, which callers must treat as
    "unknown", not as "none" -- reconcile_offers is what distinguishes them.
    """
    try:
        address = _source_address()
        resp = requests.get(f'{_HORIZON}/accounts/{address}/offers',
                            params={'limit': 200}, timeout=_TIMEOUT)
        resp.raise_for_status()
        records = resp.json().get('_embedded', {}).get('records') or []
    except Exception as e:
        print(f'[stellar_trader] could not read open offers: {e}')
        return []

    out = []
    for record in records:
        selling, buying = record.get('selling') or {}, record.get('buying') or {}
        sell_native = selling.get('asset_type') == 'native'
        buy_native = buying.get('asset_type') == 'native'
        other = buying if sell_native else selling
        is_usdc = (other.get('asset_code') == _USDC_CODE
                   and other.get('asset_issuer') == _USDC_ISSUER)
        if ours_only and not ((sell_native or buy_native) and is_usdc):
            continue
        try:
            amount = float(record.get('amount') or 0.0)
            price = float(record.get('price') or 0.0)
        except (TypeError, ValueError):
            continue
        if sell_native:
            side, amount_xlm, usd = 'ask', amount, amount * price
        else:
            # Selling USDC to buy XLM: `amount` is USDC and `price` is XLM per USDC.
            side, amount_xlm, usd = 'bid', (amount * price if price else 0.0), amount
        out.append({
            'id': str(record.get('id')),
            'side': side,
            'price': (price if sell_native else (1.0 / price if price else 0.0)),
            'amount_xlm': amount_xlm,
            'usd': usd,
            'selling': selling, 'buying': buying,
        })
    return out


def _resting_usd(offers=None):
    """(per_side_usd_dict, total_usd) across everything currently resting."""
    offers = open_offers() if offers is None else offers
    per_side = {'bid': 0.0, 'ask': 0.0}
    for offer in offers:
        per_side[offer['side']] = per_side.get(offer['side'], 0.0) + offer['usd']
    return per_side, sum(per_side.values())


def place_offer(side, usd_amount, price, *, asset='XLM', offer_id=0):
    """Rest an offer on the XLM/USDC book. Signs and submits a real pubnet transaction.

    side: 'bid' (buy XLM with USDC) or 'ask' (sell XLM for USDC). `price` is USD per XLM
    for both, which is the quoting convention everywhere else in this system -- the CLI's
    two operations express price differently (manage-sell-offer wants it per unit sold,
    manage-buy-offer per unit bought) and normalising here is what keeps a strategy from
    having to know that.

    `offer_id` non-zero REPLACES that offer atomically. Pass the id you already have
    rather than cancelling and re-placing.

    Every cap is applied here and none of them are arguments: MAX_RESTING_USD_PER_SIDE,
    MAX_RESTING_USD_TOTAL and MAX_OPEN_OFFERS are checked against Horizon's view of what
    is already resting, and MIN_QUOTE_WIDTH_BP against the live book. The caller's
    `usd_amount` is a request that gets clamped, exactly as submit_trade treats its own.

    Returns {'submitted', 'offer_id', 'tx_hash', 'price', 'price_rational', 'usd', 'reason'}.
    """
    if _paper_only():
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': 'PAPER_ONLY is set; refusing to rest a real offer'}
    if _halted():
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': 'live trading halted'}
    if side not in ('bid', 'ask'):
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': f'side must be bid or ask, got {side!r}'}
    if asset != 'XLM':
        # Multi-asset making was explicitly ruled out of scope; the caps below are all
        # sized for one pair and would have to be re-reasoned per asset.
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': 'only XLM/USDC may be quoted'}
    try:
        price = float(price)
        usd_amount = float(usd_amount)
    except (TypeError, ValueError):
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': 'price and usd_amount must be numeric'}

    # --- width floor, against the live book ---------------------------------------
    try:
        import dex_price
        book = dex_price.get_orderbook('XLM') or {}
        mid = book.get('mid')
    except Exception:
        mid = None
    if mid:
        width_bp = abs(price - mid) / mid * 10000.0
        if width_bp < MIN_QUOTE_WIDTH_BP:
            return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                    'reason': f'quote {width_bp:.2f} bp from mid is inside '
                              f'MIN_QUOTE_WIDTH_BP ({MIN_QUOTE_WIDTH_BP})'}
        # A "maker" quote on the wrong side of the mid is a taker order in disguise, and
        # would cross on submission -- spending through submit_trade's caps without
        # passing any of them.
        if (side == 'bid' and price >= mid) or (side == 'ask' and price <= mid):
            return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                    'reason': f'{side} at {price:.7f} would cross the mid {mid:.7f}'}

    # --- exposure caps, against on-chain truth ------------------------------------
    resting = open_offers()
    replacing = next((o for o in resting if o['id'] == str(offer_id)), None) if offer_id else None
    if not replacing and len(resting) >= MAX_OPEN_OFFERS:
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': f'{len(resting)} offers already open (max {MAX_OPEN_OFFERS})'}
    per_side, total = _resting_usd(resting)
    # A replace frees its own notional before it re-books it.
    if replacing:
        per_side[replacing['side']] -= replacing['usd']
        total -= replacing['usd']
    room_side = max(0.0, MAX_RESTING_USD_PER_SIDE - per_side.get(side, 0.0))
    room_total = max(0.0, MAX_RESTING_USD_TOTAL - total)
    capped_usd = min(usd_amount, room_side, room_total)

    # The daily spend cap binds the BID side, at placement. A resting offer is not a spend
    # until it fills -- record_fill_spend below is what books it -- but resting more than
    # the remaining daily budget means the budget can be blown by a fill we have already
    # committed to and cannot refuse. Sizing the offer to the remaining budget is the only
    # point at which this cap can still be enforced.
    #
    # This wiring is the whole reason MAX_DAILY_USD did not bound a maker at all:
    # _record_spend is called from submit_trade and from nowhere else, so before this
    # every offer fill was invisible to _daily_spent() and the cap read as satisfied no
    # matter how much traded.
    if side == 'bid':
        capped_usd = min(capped_usd, max(0.0, MAX_DAILY_USD - _daily_spent()))

    # --- settle-ability, against the real balance ---------------------------------
    try:
        account = _account(_source_address())
    except Exception as e:
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': f'could not read account: {e}'}
    if side == 'ask':
        # _sellable_xlm already nets out selling_liabilities, so the XLM this very
        # function has previously committed is not counted as available twice.
        available_usd = _sellable_xlm(account) * price
    else:
        free_usdc = _free_balance(account, _USDC_CODE, _USDC_ISSUER)
        if free_usdc is None:
            return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                    'reason': 'no USDC trustline'}
        available_usd = free_usdc
    capped_usd = min(capped_usd, available_usd)
    if capped_usd <= 0:
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': 'no room under the resting caps or no free balance'}

    try:
        n, d = _price_rational(price)
    except ValueError as e:
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': str(e)}
    amount_xlm = capped_usd / price

    if side == 'ask':
        argv = ['stellar', 'tx', 'new', 'manage-sell-offer',
                '--source', _IDENTITY, '--network', _NETWORK,
                '--selling', _sep11('XLM'), '--buying', _USDC_ASSET,
                '--amount', str(_to_stroops(amount_xlm)),
                '--price', f'{n}:{d}', '--offer-id', str(offer_id)]
    else:
        # manage-buy-offer: --amount is the BUYING asset (XLM) and --price is the price of
        # one unit of the buying asset in the selling asset, i.e. USD per XLM. Same
        # rational as the ask, which is exactly why this is normalised here.
        argv = ['stellar', 'tx', 'new', 'manage-buy-offer',
                '--source', _IDENTITY, '--network', _NETWORK,
                '--selling', _USDC_ASSET, '--buying', _sep11('XLM'),
                '--amount', str(_to_stroops(amount_xlm)),
                '--price', f'{n}:{d}', '--offer-id', str(offer_id)]

    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=_TX_TIMEOUT)
    except Exception as e:
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': str(e)}
    if result.returncode != 0:
        return {'submitted': False, 'offer_id': None, 'tx_hash': None, 'usd': 0.0,
                'reason': (result.stderr or '').strip() or 'manage-offer failed'}

    combined = f'{result.stdout}\n{result.stderr}'
    match = re.search(r'\b[0-9a-fA-F]{64}\b', combined)
    tx_hash = match.group(0) if match else _most_recent_tx_hash(_source_address())

    # The id of what is now resting, read back from Horizon rather than parsed out of CLI
    # output -- the same reason _swap cross-references Horizon for its hash. A replace
    # keeps its id; a create gets a new one, and matching on (side, price) is the only
    # way to name it without trusting a format that has already differed between CLI
    # versions in this project.
    new_id = str(offer_id) if offer_id else None
    if not new_id:
        for offer in open_offers():
            if offer['side'] == side and abs(offer['price'] - price) / price < 1e-6:
                new_id = offer['id']
                break

    _log_pubnet_trade(f'offer_{side}', round(capped_usd, 7), round(amount_xlm, 7),
                      tx_hash, reason=f'rest @ {n}:{d} (id {new_id})')
    return {'submitted': True, 'offer_id': new_id, 'tx_hash': tx_hash,
            'price': price, 'price_rational': f'{n}:{d}',
            'usd': capped_usd, 'reason': None}


def record_fill_spend(usd_amount, side, *, asset='XLM'):
    """Book a detected offer fill against the daily spend budget.

    Called by quote_executor when reconciliation finds a bid that shrank. Buys only, for
    the same reason submit_trade records buys only: a sell is not a spend, and counting it
    as one meant an exit consumed the very budget the next entry needed.

    Separate from placement because the two are separated in time by design -- an offer
    may rest for MAX_OFFER_AGE_S and never fill, and charging the budget at placement
    would let an unfilled quote exhaust a day.
    """
    if side != 'bid':
        return 0.0
    try:
        usd_amount = float(usd_amount)
    except (TypeError, ValueError):
        return 0.0
    if usd_amount <= 0 or _paper_only():
        return 0.0
    _record_spend(usd_amount, _spec_of_asset(asset))
    return usd_amount


def _spec_of_asset(asset):
    """'XLM' or a canonical spec, for the daily-spend ledger's per-asset key."""
    try:
        import assets
        return assets.normalize(asset)
    except Exception:
        return 'XLM'


def daily_spend_status():
    """{'spent_usd', 'cap_usd', 'remaining_usd'} over the trailing 24h, all assets."""
    spent = _daily_spent()
    return {'spent_usd': round(spent, 4), 'cap_usd': MAX_DAILY_USD,
            'remaining_usd': round(max(0.0, MAX_DAILY_USD - spent), 4)}


def cancel_offer(offer_id, side=None, *, asset='XLM'):
    """Delete one resting offer. An amount of 0 is Stellar's cancel.

    `side` is optional and only used to pick the operation when the offer is no longer on
    chain (already filled): the id is enough otherwise, because open_offers() knows which
    side it is. An id that is not resting is reported as already gone rather than as a
    failure -- a cancel racing a fill is normal, not an error, and treating it as one is
    how a shutdown path gets stuck retrying.
    """
    if _paper_only():
        return {'cancelled': False, 'reason': 'PAPER_ONLY is set'}
    resting = {o['id']: o for o in open_offers()}
    offer = resting.get(str(offer_id))
    if offer is None:
        return {'cancelled': True, 'offer_id': str(offer_id),
                'reason': 'not resting (already filled or cancelled)'}
    if offer['side'] == 'ask':
        argv = ['stellar', 'tx', 'new', 'manage-sell-offer',
                '--source', _IDENTITY, '--network', _NETWORK,
                '--selling', _sep11('XLM'), '--buying', _USDC_ASSET,
                '--amount', '0', '--price', '1:1', '--offer-id', str(offer_id)]
    else:
        argv = ['stellar', 'tx', 'new', 'manage-buy-offer',
                '--source', _IDENTITY, '--network', _NETWORK,
                '--selling', _USDC_ASSET, '--buying', _sep11('XLM'),
                '--amount', '0', '--price', '1:1', '--offer-id', str(offer_id)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=_TX_TIMEOUT)
    except Exception as e:
        return {'cancelled': False, 'offer_id': str(offer_id), 'reason': str(e)}
    if result.returncode != 0:
        return {'cancelled': False, 'offer_id': str(offer_id),
                'reason': (result.stderr or '').strip() or 'cancel failed'}
    _log_pubnet_trade('offer_cancel', 0.0, 0.0, '', reason=f'id {offer_id}')
    return {'cancelled': True, 'offer_id': str(offer_id), 'reason': None}


def cancel_all_offers():
    """The maker's wind_down: leave nothing resting. Called before wind_down(), not after.

    ORDER MATTERS AND IS NOT A STYLE CHOICE. wind_down sizes its chunks against
    _sellable_xlm, which now nets out selling_liabilities -- so with asks still resting it
    would under-sell or fail outright and report the position as un-liquidated. Worse, any
    offer left resting can fill AFTER the handover, opening a position on behalf of a
    strategy that is no longer live and that nothing in the system is watching.

    Returns {'ok', 'cancelled', 'remaining', 'failures'}. `ok` is False while anything is
    still resting, so a caller can loop, and `remaining` is re-read from Horizon rather
    than inferred from what we think we cancelled.
    """
    if _paper_only():
        return {'ok': True, 'cancelled': 0, 'remaining': 0, 'failures': [],
                'reason': 'PAPER_ONLY is set; nothing can be resting'}
    cancelled, failures = 0, []
    for offer in open_offers():
        result = cancel_offer(offer['id'], offer['side'])
        if result.get('cancelled'):
            cancelled += 1
        else:
            failures.append({'id': offer['id'], 'reason': result.get('reason')})
    remaining = len(open_offers())
    return {'ok': remaining == 0, 'cancelled': cancelled, 'remaining': remaining,
            'failures': failures}


def reconcile_offers(expected):
    """Compare what we believe is resting against what Horizon says, and report fills.

    `expected` is {offer_id: {'side', 'price', 'usd', 'placed_ts'}} -- whatever the caller
    last placed. This is the function that makes polling safe. Fill detection by polling
    is lossy at the edges by construction: between two polls an offer can fill partially,
    fill fully, or fill and be replaced, and the poll sees only the endpoints. What makes
    that acceptable is not a faster poll, it is reconciling against on-chain truth and
    treating every disagreement as something to be acted on rather than logged.

    Returns:
        {'fills':    [{'offer_id', 'side', 'price', 'filled_usd'}]   shrank or vanished
         'unknown':  [{'id', ...}]                  resting but we did not place it
         'stale':    [{'offer_id', 'age_s', ...}]   older than MAX_OFFER_AGE_S
         'resting':  [...]                          the current on-chain view
         'ok':       bool}                          nothing unknown and nothing stale

    A vanished offer is reported as a fill for its whole remaining size. It might instead
    have been a cancel we issued, which is why the caller must drop an offer from
    `expected` when it cancels it -- and why this returns the raw disagreement rather than
    trying to guess. Cross-check against /accounts/<addr>/trades, which attributes trades
    to offer ids, before treating a fill number as settled.
    """
    resting = open_offers()
    by_id = {o['id']: o for o in resting}
    now = time.time()
    fills, stale = [], []
    for offer_id, record in (expected or {}).items():
        offer_id = str(offer_id)
        was = float(record.get('usd') or 0.0)
        still = by_id.get(offer_id)
        now_usd = float(still['usd']) if still else 0.0
        if now_usd < was - 1e-9:
            fills.append({'offer_id': offer_id, 'side': record.get('side'),
                          'price': record.get('price'),
                          'filled_usd': round(was - now_usd, 7),
                          'fully': still is None})
        if still and record.get('placed_ts'):
            age = now - float(record['placed_ts'])
            if age > MAX_OFFER_AGE_S:
                stale.append({'offer_id': offer_id, 'age_s': round(age, 1),
                              'side': still['side'], 'price': still['price']})
    unknown = [o for o in resting if o['id'] not in {str(k) for k in (expected or {})}]
    return {'fills': fills, 'unknown': unknown, 'stale': stale, 'resting': resting,
            'ok': not unknown and not stale}


def offer_status():
    """Read-only summary of the resting book, for the operator surface and live_report."""
    resting = open_offers()
    per_side, total = _resting_usd(resting)
    return {
        'open': len(resting),
        'max_open': MAX_OPEN_OFFERS,
        'resting_usd': {k: round(v, 4) for k, v in per_side.items()},
        'resting_usd_total': round(total, 4),
        'caps': {'per_side': MAX_RESTING_USD_PER_SIDE,
                 'total': MAX_RESTING_USD_TOTAL,
                 'max_offer_age_s': MAX_OFFER_AGE_S,
                 'min_quote_width_bp': MIN_QUOTE_WIDTH_BP},
        'offers': resting,
    }


if __name__ == "__main__":
    # The only operator surface for this module. It printed balances and nothing about
    # the multi-asset state, which meant every question this system's live path can
    # actually fail on -- is there a trustline, is a leg stuck, are we halted, how much
    # headroom is there for one more trustline -- had to be answered by hand from
    # Horizon and four dotfiles. Everything below is read-only.
    address = _source_address()
    account = _account(address)
    xlm = _asset_balance(account, "XLM")
    usdc = _asset_balance(account, "USDC")
    subentries = account.get("subentry_count", 0)
    print(f"claudio ({address})")
    print(f"  XLM:  {xlm:.7f} ({subentries} subentries; spendable above reserve: "
          f"{_spendable_xlm(account):.7f}, sellable: {_sellable_xlm(account):.7f})")
    print(f"  USDC: {'no trustline' if usdc is None else f'{usdc:.7f}'}")
    print(f"  daily spend used: ${_daily_spent():.2f} / ${MAX_DAILY_USD:.2f}")

    # Headroom for one more trustline, using ensure_trustline's own arithmetic so the two
    # cannot disagree about whether the next asset is affordable.
    after = xlm - (2.5 + subentries + 1) * _BASE_RESERVE_XLM - _FEE_BUFFER_XLM
    ok = after >= MIN_XLM_OPERATING_BUFFER
    print(f"  one more trustline would leave {after:.4f} XLM spendable "
          f"(need >= {MIN_XLM_OPERATING_BUFFER}): {'OK' if ok else 'REFUSED -- fund first'}")

    ours = _read_json(_TRUSTLINES_PATH, {})
    print(f"  trustlines opened by this system: {len(ours)}/{MAX_SYSTEM_TRUSTLINES}"
          f"{' -- ' + ', '.join(sorted(ours)) if ours else ' (none)'}")

    positions = open_positions()
    if positions:
        for p in positions:
            print(f"    holding {p['amount']} {p['code']} ({p['spec']})")
    else:
        print("    no non-XLM legs held")

    stuck = _stuck_positions()
    if stuck:
        print(f"  STUCK: {len(stuck)} leg(s) -- {', '.join(sorted(stuck))}")
    print(f"  halted: {'YES -- ' + str(_HALT_PATH) if _halted() else 'no'}")
    print("(read-only status check — submit_trade()/wind_down() are not called here)")

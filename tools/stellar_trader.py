#!/usr/bin/env python3
"""Real-money XLM/USDC trading on Stellar pubnet using the `claudio` identity.

Design: ../../pubnet-plan.md. This module (not any strategy's config.json, not
template_repo/main.py) is the actual safety boundary that plan describes: MAX_TRADE_USD
and MAX_DAILY_USD live only here, are never expressed as caller-supplied parameters, and
are enforced regardless of what a (possibly LLM-revised) strategy asks for.

Shells out to the `stellar` CLI for signing/submission, same shell-out pattern as
reflector_oracle.py, and to Horizon's REST API for balance queries — classic XLM/USDC
balances live in account trustlines, not a Soroban contract, so `contract invoke` (what
reflector_oracle.py uses) doesn't apply here.

Confirmed live against `claudio`
(GBTFQJ6VARJYI2C6JLPUXQ4CAKRNJF3KEYXXJ5T74DV47RSSNIJCH5VM), via a plain Horizon GET, not
assumed:
- already holds a USDC trustline — issuer GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN,
  the well-known Circle-issued USDC on pubnet. Contrary to pubnet-plan.md's open question,
  no manual trustline setup is needed before Rollout phase 1 — it's already there.
- currently funded with ~27.9 XLM and ~14.0 USDC (also holds some BLND, irrelevant here).

Trading itself uses a self-payment `path-payment-strict-send` (source == destination ==
claudio's own address) — the standard way to take a market-order-like swap off the
Stellar DEX orderbook from the CLI, as opposed to manage-buy-offer/manage-sell-offer,
which post a *resting* limit order that may not fill immediately or in full.

CAUTION: submit_trade()/wind_down() sign and submit real pubnet transactions. Per
pubnet-plan.md Rollout phase 1, exercise them manually from a REPL with MAX_TRADE_USD
this low before wiring them into monitor.py/main.py — running this file directly
(`python3 stellar_trader.py`) only prints a read-only status report, it does not trade.
"""
import json
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

# Single hard-coded trading pair. Never accepted as a parameter from any caller — see
# the "Decided" note in pubnet-plan.md's Safety caps section: submit_trade's signature
# (side, usd_amount) has no asset argument, so there's structurally nothing for a
# revision to override.
_USDC_CODE = "USDC"
_USDC_ISSUER = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
_USDC_ASSET = f"{_USDC_CODE}:{_USDC_ISSUER}"

# Rollout phase 1 starter values (pubnet-plan.md) — deliberately tiny until behavior is
# trusted, then raised by a human editing this file directly. Never read from any
# strategy's config.json.
MAX_TRADE_USD = 1.0
MAX_DAILY_USD = 5.0

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

BASE_DIR = Path('/opt/trades')
BASE_DIR.mkdir(parents=True, exist_ok=True)
_LEDGER_PATH = BASE_DIR / '.pubnet_ledger.json'
_LIVE_STRATEGY_PATH = Path('/opt/live_strategy.json')


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


def _asset_balance(account_json, code):
    """Real on-chain balance of `code` ('XLM' or 'USDC'). None means no trustline
    (only possible for USDC — every account has native XLM), distinct from a real
    zero balance."""
    if code == "XLM":
        for b in account_json["balances"]:
            if b["asset_type"] == "native":
                return float(b["balance"])
        return 0.0
    for b in account_json["balances"]:
        if b.get("asset_code") == _USDC_CODE and b.get("asset_issuer") == _USDC_ISSUER:
            return float(b["balance"])
    return None


def _spendable_xlm(account_json):
    """XLM balance minus the account's minimum reserve and a small fee buffer, so a
    sell/wind_down chunk never tries to spend down to a balance the network will
    reject once its own transaction fee is deducted."""
    balance = _asset_balance(account_json, "XLM")
    reserve = (2 + account_json.get("subentry_count", 0)) * _BASE_RESERVE_XLM
    return max(0.0, balance - reserve - _FEE_BUFFER_XLM)


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


def _log_pubnet_trade(action, amount_usd, amount_xlm, tx_hash, reason=None):
    entry = {
        "timestamp": time.time(),
        "action": action,
        "amount_usd": amount_usd,
        "amount_xlm": amount_xlm,  # estimated from the pre-trade price, not the exact fill
        "tx_hash": tx_hash,
        "reason": reason,
    }
    log_path = BASE_DIR / f"{_current_live_name()}.pubnet.log"
    with open(log_path, 'a') as f:
        f.write(json.dumps(entry) + "\n")


def _read_ledger():
    if not _LEDGER_PATH.exists():
        return []
    try:
        return json.loads(_LEDGER_PATH.read_text())
    except Exception:
        return []


def _daily_spent():
    cutoff = time.time() - 86400
    return sum(e["amount_usd"] for e in _read_ledger() if e["ts"] > cutoff)


def _record_spend(amount_usd):
    cutoff = time.time() - 86400
    entries = [e for e in _read_ledger() if e["ts"] > cutoff]
    entries.append({"ts": time.time(), "amount_usd": amount_usd})
    _LEDGER_PATH.write_text(json.dumps(entries))


def _to_stroops(amount):
    return max(0, round(amount * 10_000_000))


def _swap(*, spend_code, send_amount, dest_code, dest_min):
    """Self-payment path-payment-strict-send: swap `send_amount` of `spend_code` for
    at least `dest_min` of `dest_code`. `spend_code`/`dest_code` are always 'XLM' or
    'USDC', supplied only by submit_trade/wind_down below — never by a caller.
    """
    address = _source_address()
    send_asset = "native" if spend_code == "XLM" else _USDC_ASSET
    dest_asset = "native" if dest_code == "XLM" else _USDC_ASSET
    result = subprocess.run(
        [
            "stellar", "tx", "new", "path-payment-strict-send",
            "--source", _IDENTITY,
            "--network", _NETWORK,
            "--send-asset", send_asset,
            "--send-amount", str(_to_stroops(send_amount)),
            "--destination", address,
            "--dest-asset", dest_asset,
            "--dest-min", str(_to_stroops(dest_min)),
        ],
        capture_output=True, text=True, timeout=_TX_TIMEOUT,
    )
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


def submit_trade(side: str, usd_amount: float) -> dict:
    """Sign and submit a real XLM/USDC trade on pubnet using the `claudio` identity.

    side: 'buy' (spend USDC for XLM) or 'sell' (spend XLM for USDC). usd_amount:
    requested USD notional — silently clamped to MAX_TRADE_USD, the remaining
    MAX_DAILY_USD budget, and claudio's real on-chain balance of whatever asset the
    trade spends, regardless of what the caller asks for.

    Returns {'submitted': bool, 'tx_hash': str|None, 'amount_usd': float, 'reason': str|None}.
    """
    if side not in ("buy", "sell"):
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": f"invalid side {side!r}"}

    price = get_price()
    if price is None:
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": "no price available"}

    capped_usd = max(0.0, float(usd_amount))
    capped_usd = min(capped_usd, MAX_TRADE_USD, max(0.0, MAX_DAILY_USD - _daily_spent()))

    account = _account(_source_address())
    spend_code = "USDC" if side == "buy" else "XLM"
    if spend_code == "XLM":
        available_usd = _spendable_xlm(account) * price
    else:
        usdc_balance = _asset_balance(account, "USDC")
        if usdc_balance is None:
            return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                    "reason": "no trustline"}
        available_usd = usdc_balance
    capped_usd = min(capped_usd, available_usd)

    if capped_usd <= 0:
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0,
                "reason": "insufficient balance or caps exhausted"}

    dest_code = "XLM" if side == "buy" else "USDC"
    send_amount = capped_usd if spend_code == "USDC" else capped_usd / price
    dest_amount = capped_usd / price if dest_code == "XLM" else capped_usd
    dest_min = dest_amount * (1 - _SLIPPAGE)

    try:
        tx_hash = _swap(spend_code=spend_code, send_amount=send_amount,
                         dest_code=dest_code, dest_min=dest_min)
    except Exception as e:
        return {"submitted": False, "tx_hash": None, "amount_usd": 0.0, "reason": str(e)}

    _record_spend(capped_usd)
    filled_xlm = dest_amount if side == "buy" else send_amount
    _log_pubnet_trade(side, capped_usd, filled_xlm, tx_hash)
    return {"submitted": True, "tx_hash": tx_hash, "amount_usd": capped_usd, "reason": None}


def wind_down() -> dict:
    """Liquidate claudio's entire real XLM position back to USDC.

    Called by monitor.py's promote_live_strategy when swapping the live strategy — not
    importable from template_repo/, same restriction as submit_trade. Exactly one
    strategy trades live at a time, so claudio's on-chain XLM balance at demotion time
    is entirely attributable to the outgoing live strategy; there's no per-strategy
    position to track, just flatten whatever's on the account.

    Sells in MAX_TRADE_USD-sized chunks to avoid slippage from dumping a large position
    in one market sell, looping until the remaining balance is below the dust threshold
    or _MAX_WIND_DOWN_CHUNKS_PER_CALL is hit. NOT gated by MAX_DAILY_USD — that cap
    bounds loss from bad *buys*; applying it to a de-risking sell could trap claudio
    holding an unhedged position for days with no strategy live. MAX_TRADE_USD still
    applies per chunk, purely for slippage control.

    Returns {'liquidated': bool, 'remaining_xlm': float, 'chunks': int, 'reason': str|None}.
    """
    chunks = 0
    while chunks < _MAX_WIND_DOWN_CHUNKS_PER_CALL:
        account = _account(_source_address())
        remaining = _spendable_xlm(account)
        if remaining <= _XLM_DUST:
            return {"liquidated": True, "remaining_xlm": remaining, "chunks": chunks, "reason": None}

        price = get_price()
        if price is None:
            return {"liquidated": False, "remaining_xlm": remaining, "chunks": chunks,
                     "reason": "no price available"}

        chunk_usd = min(remaining * price, MAX_TRADE_USD)
        send_amount = chunk_usd / price
        dest_min = chunk_usd * (1 - _SLIPPAGE)
        try:
            tx_hash = _swap(spend_code="XLM", send_amount=send_amount,
                             dest_code="USDC", dest_min=dest_min)
        except Exception as e:
            return {"liquidated": False, "remaining_xlm": remaining, "chunks": chunks, "reason": str(e)}

        _log_pubnet_trade("wind_down_sell", chunk_usd, send_amount, tx_hash)
        chunks += 1

    return {"liquidated": False, "remaining_xlm": _spendable_xlm(_account(_source_address())),
            "chunks": chunks, "reason": "chunk limit reached this cycle, will retry"}


if __name__ == "__main__":
    address = _source_address()
    account = _account(address)
    xlm = _asset_balance(account, "XLM")
    usdc = _asset_balance(account, "USDC")
    print(f"claudio ({address})")
    print(f"  XLM:  {xlm:.7f} (spendable above reserve: {_spendable_xlm(account):.7f})")
    print(f"  USDC: {'no trustline' if usdc is None else f'{usdc:.7f}'}")
    print(f"  daily spend used: ${_daily_spent():.2f} / ${MAX_DAILY_USD:.2f}")
    print("(read-only status check — submit_trade()/wind_down() are not called here)")

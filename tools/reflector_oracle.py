#!/usr/bin/env python3
"""Price feed for Stellar-network assets via Reflector's "Pulse" oracle.

Reflector (reflector.network) runs a family of on-chain Soroban oracles on Stellar
pubnet. The docs don't publish a name-to-contract-ID table, so the constants below
were confirmed live with `stellar contract invoke` rather than scraped:

    stellar contract invoke --id CALI2BYU2JE6WVRUFYTS6MSBNEHGJ35P4AVCZYF3B6QOE3QKOB2PLE6M \
        --source <any-identity> --network pubnet -- base       # -> Stellar(USDC SAC)
    ... -- decimals    # -> 14
    ... -- resolution  # -> 300  (5 min — the "Pulse" free/public tier's tick period)

That contract is the "Stellar Pubnet Pulse" oracle: it tracks assets that trade on
Stellar's own DEX (returned by its `assets()` call as a list of Stellar Asset
Contract addresses), quoted in USDC.

Queries shell out to the `stellar` CLI (contract simulation only — read-only, no fee,
nothing signed or submitted) since that's what's installed in this environment, rather
than pulling in a Soroban Python SDK.
"""
import json
import subprocess

_ORACLE_CONTRACT = "CALI2BYU2JE6WVRUFYTS6MSBNEHGJ35P4AVCZYF3B6QOE3QKOB2PLE6M"
_NETWORK = "pubnet"
_DECIMALS = 14  # oracle.decimals() — fixed for the life of the deployed contract
_IDENTITY = "reflector-reader"  # local-only key, deliberately never funded: invoke
                                 # here only ever simulates a read, nothing is signed

_XLM_SAC = "CAS3J7GYLGXMF6TDJBBYYSE3HQ6BBSMLNUQ34T6TZMYMW2EVH34XOWMA"  # native XLM, pubnet

_TIMEOUT = 20


def _ensure_identity():
    check = subprocess.run(
        ["stellar", "keys", "address", _IDENTITY],
        capture_output=True, text=True, timeout=_TIMEOUT,
    )
    if check.returncode != 0:
        subprocess.run(
            ["stellar", "keys", "generate", _IDENTITY],
            capture_output=True, text=True, timeout=_TIMEOUT, check=True,
        )


def _invoke(*args):
    _ensure_identity()
    result = subprocess.run(
        [
            "stellar", "contract", "invoke",
            "--id", _ORACLE_CONTRACT,
            "--source", _IDENTITY,
            "--network", _NETWORK,
            "--",
            *args,
        ],
        capture_output=True, text=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "stellar contract invoke failed")
    return result.stdout.strip()


def get_price(asset_contract=_XLM_SAC):
    """Last Reflector Pulse price for a Stellar Asset Contract address, in USDC (~USD).

    Defaults to native XLM. Returns None (after printing a diagnostic) if the CLI is
    missing, the network is unreachable, or the oracle has no price for this asset.
    """
    try:
        raw = _invoke("lastprice", "--asset", json.dumps({"Stellar": asset_contract}))
        data = json.loads(raw)
    except Exception as e:
        print(f"[reflector_oracle] error: {e}")
        return None
    if not data:
        print(f"[reflector_oracle] no price for {asset_contract}")
        return None
    return int(data["price"]) / 10 ** _DECIMALS


if __name__ == "__main__":
    price = get_price()
    print(f"XLM/USDC: {price}" if price is not None else "failed")

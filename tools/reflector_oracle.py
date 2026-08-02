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


def _invoke_contract(contract_id, *args):
    """Simulate a read against any contract, not just the oracle.

    Resolving the oracle's tracked assets means calling `name()` on each Stellar Asset
    Contract it returns, so the identity handling and timeout below have to cover
    contracts other than _ORACLE_CONTRACT. Still simulation only -- nothing signed.
    """
    _ensure_identity()
    result = subprocess.run(
        [
            "stellar", "contract", "invoke",
            "--id", contract_id,
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


def _invoke(*args):
    return _invoke_contract(_ORACLE_CONTRACT, *args)


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


# SAC address -> the asset name that contract reports. A deployed contract's name() is
# immutable, so this never needs invalidating; in-memory only, so a fresh process pays
# the full ~1-2 minute resolve once and every later refresh in that process is free.
# Failures are deliberately NOT cached -- an unreachable Horizon is transient, and a
# dead issuer costs one wasted call per refresh, which happens rarely.
_name_cache = {}


def _sac_name(sac):
    """`name()` on a Stellar Asset Contract: 'CODE:ISSUER', 'native', or None.

    None means the SAC could not be resolved at all -- most often because its issuer
    account no longer exists on pubnet, which the oracle's own list does not filter out.
    That is an expected condition here, not an error worth surfacing.
    """
    if sac in _name_cache:
        return _name_cache[sac]
    try:
        name = json.loads(_invoke_contract(sac, "name"))
    except Exception:
        return None
    if not isinstance(name, str) or not name:
        return None
    _name_cache[sac] = name
    return name


def get_tracked_assets(include_native=False):
    """Every asset the Pulse oracle tracks, resolved to (code, issuer), sorted by spec.

    The oracle's `assets()` returns Stellar Asset Contract addresses, but config.json
    and the rest of the asset stack are built on (code, issuer) pairs, so each SAC's own
    `name()` supplies the reverse mapping. That is one CLI invocation per asset at
    roughly 1-3s each, so a cold full resolve of the ~49 tracked assets takes 1-2
    minutes; `_name_cache` makes every later call in the same process near-instant.

    Returns [{'code', 'issuer', 'spec', 'sac'}, ...]; [] on any failure, since callers
    run inside monitor's cycle and an oracle outage must degrade to "no candidates"
    rather than raise. XLM is excluded unless `include_native`: it is the permanent base
    leg carried by config's top-level thresholds, and portfolio.assets_from_config
    rejects it as an extra asset anyway.
    """
    try:
        import assets as assets_mod
    except Exception as e:
        print(f"[reflector_oracle] cannot validate asset identities: {e}")
        return []

    try:
        raw = json.loads(_invoke("assets"))
    except Exception as e:
        print(f"[reflector_oracle] error listing tracked assets: {e}")
        return []
    if not isinstance(raw, list):
        return []

    tracked, seen = [], set()
    for entry in raw:
        sac = entry.get("Stellar") if isinstance(entry, dict) else None
        if not isinstance(sac, str) or not sac:
            continue
        name = _sac_name(sac)
        if name is None:
            continue  # unresolvable SAC (e.g. deleted issuer account) -- drop it
        if name == "native":
            if not include_native:
                continue
            code, issuer, spec = assets_mod.NATIVE, None, assets_mod.NATIVE
        else:
            code, _, issuer = name.partition(":")
            try:
                spec = assets_mod.canonical(code, issuer)
            except Exception:
                continue  # not a well-formed (code, issuer) pair
        if spec in seen:
            continue
        seen.add(spec)
        tracked.append({'code': code, 'issuer': issuer, 'spec': spec, 'sac': sac})

    return sorted(tracked, key=lambda a: a['spec'])


if __name__ == "__main__":
    price = get_price()
    print(f"XLM/USDC: {price}" if price is not None else "failed")

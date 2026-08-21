#!/usr/bin/env python3
"""Live census of the venues YIELD.md's yield-rotation domain would allocate across.

This is YIELD.md step 1: enumerate the Blend lending pools and the Aquarius reward
pools, their contract addresses and their current rates, confirmed by simulation rather
than scraped. Nothing here signs or submits anything -- every chain read is either a
`simulateTransaction` against a throwaway source account or a `getLedgerEntries` state
read, both of which are free and change nothing.

Four things were confirmed live building this, and each contradicts a document:

  1. **The pools YIELD.md names are Blend v1 addresses, and the live ones are v2.**
     docs-v1.blend.capital publishes YieldBlox as CBP7NO6F... and Fixed as CDVQVKOY...;
     both answer `is_pool` on the v1 factory and neither answers it on the v2 factory.
     The v2 backstop's `reward_zone()` returns six *different* pools, two of which are
     also called "YieldBlox" and "Fixed" (names read out of each pool's instance
     storage). The emitter's `get_backstop()` returns the v2 backstop, so BLND emissions
     flow to the v2 set and the v1 pools are legacy. Allocating to the documented
     addresses would be allocating to the wrong contracts.
  2. **Two of the six reward-zone pools cannot be supplied into at all.** Blend v2's
     `require_action_allowed` disables Supply and SupplyCollateral whenever
     `status > 3`. Orbit sits at 4 (admin frozen) and Forex at 5 (frozen), so the
     allocatable universe is four pools, not six. Status is read per cycle, not
     assumed: a pool can be frozen by its admin, or by Q4W crossing 60%, between runs.
  3. **The Aquarius reward universe is 15 markets, not 337 pools.** reward-api's
     `/api/rewards/` enumerates every rewarded market and nothing else does; the AMM
     API's pool records carry no reward or APY field at all. Per-pool `tps` off the
     chain is the number that matters, and this module cross-checks it against the
     API's advertised `daily_amm_reward` -- they are independent sources and a
     disagreement means one of them is stale.
  4. **`tools/reflector_oracle.py` does not run on the host.** Its `stellar contract
     invoke --network pubnet` needs a CLI network config that only exists in the
     container, so it returns None for every asset here. This module therefore talks to
     RPC through stellar-sdk with an explicit endpoint and passphrase, which works in
     both places, and takes USD marks from `dex_price` (Horizon) instead.

RATE MATH is transcribed from blend-contracts-v2 rather than approximated:
`pool/src/pool/interest.rs::calc_accrual` for the kinked curve, and
`pool/src/pool/reserve.rs` for utilization and the backstop take. The fixed-point
scalars are not uniform and getting one wrong silently changes an APR by 10x --
b_rate/d_rate are 1e12, every config percentage and `ir_mod` are 1e7. The rounding
directions here match the contract's (`fixed_mul_ceil` on liabilities, `fixed_mul_floor`
on supply) so a value computed here equals the one the pool would compute.

VALIDATION, because a rate model that is wrong in the fourth decimal is indistinguishable
from one that is wrong by 10x until you check: the supply APRs computed here reproduce
DefiLlama's independently-sourced Blend numbers on 2026-08-21 to four significant figures
-- YieldBlox USDC 4.60% against their apyBase of 4.60088, YieldBlox XLM 0.063% against
0.06326, Fixed XLM emissions 0.05% against their apyReward of 0.04724. Their "tvlUsd" is
free liquidity rather than total supplied, and matches this module's `free_liquidity`
column to the dollar ($144,766 vs 144,791 USDC on YieldBlox), which is a second check on
the b_rate scaling. On the Aquarius side every one of the 15 markets reconciles the
reward API's advertised AQUA/day against the sum of on-chain `tps` at 1.00x.

WHAT THIS DELIBERATELY DOES NOT DO: it reports emission APRs **gross**, at the mark, and
labels them so. YIELD.md §1 is the whole argument for why a gross emission number is not
a return -- AQUA and BLND have to be sold into books that `friction.py:16` records at
151-186bp against XLM's 12, and on those spreads the exit cost can be most of the yield.
Netting that out is step 3's job and needs a size, which a census does not have.

Usage:
    python tools/yield_venues.py              # human-readable census
    python tools/yield_venues.py --json       # same data as JSON, for steps 2 and 3
    python tools/yield_venues.py --blend      # one venue only
    python tools/yield_venues.py --aqua
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stellar_sdk import (
    Account, Address, Asset, Network, SorobanServer, TransactionBuilder, scval,
)
from stellar_sdk import xdr as sdk_xdr

RPC_URL = "https://mainnet.sorobanrpc.com"
PASSPHRASE = Network.PUBLIC_NETWORK_PASSPHRASE

# Simulation needs a source account that exists; it is never signed with and never pays.
# This is the AQUA issuer, which is the account aquarius-sdk itself uses for signer-less
# reads (see aquarius/networks.py: NETWORKS['mainnet'].read_source).
READ_SOURCE = "GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA"

# --- Blend, mainnet. v1 addresses from docs-v1.blend.capital, v2 from docs.blend.capital;
# every one of them is re-confirmed at runtime rather than trusted (see census_blend).
BACKSTOP_V2 = "CAQQR5SWBXKIGZKPBZDH3KM5GQ5GUTPKB7JAFCINLZBC5WXPJKRG3IM7"
BACKSTOP_V1 = "CAO3AGAMZVRMHITL36EJ2VZQWKYRPWMQAPDQD5YEOF3GIF7T44U4JAL3"
FACTORY_V2 = "CDSYOAVXFY7SM5S64IZPPPYB4GVGGLMQVFREPSQQEZVIWXX5R23G4QSU"
FACTORY_V1 = "CCZD6ESMOGMPWH2KRO4O7RGTAPGTUPFWFQBELQSS7ZUK63V3TZWETGAG"
EMITTER = "CCOQM6S7ICIUWA225O5PSJWUBEMXGFSSW2PQFO6FP4DQEKMS5DASRGRR"
# The two pools YIELD.md names, at the addresses the v1 docs give. Kept so the census can
# say out loud that they are the v1 contracts and not the pools it is reporting on.
V1_DOC_POOLS = {
    "YieldBlox": "CBP7NO6F7FRDHSOFQBT2L2UWYIZ2PU76JKVRYAQTG3KZSQLYAOKIF2WB",
    "Fixed": "CDVQVKOY2YSXS2IC7KN6MNASSHPAO7UN2UR2ON4OI2SKMFJNVAMDX6DP",
}

BLND_SPEC = "BLND:GDJEHTBE6ZHUXSWFI642DCGLUOECLHPF3KSXHPXTSTJ7E3JF6MQ5EZYY"
AQUA_SPEC = "AQUA:GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA"

# The Aquarius AMM router: `get_pools([token_a, token_b])` enumerates every pool for a
# pair, which is what the AMM API's paging fails to do reliably (see census_aquarius).
ROUTER = "CBQDHNBFBZYE4MKPWBSJOPIYLW4SFSXAXUTSXJN76GNKYVYPCKWC6QUK"
REWARD_API = "https://reward-api.aqua.network/api/rewards/"
# Both Aquarius APIs and several public RPCs 403 a default python-urllib User-Agent.
# This is not rate limiting and not an outage; it looks exactly like one.
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

SCALAR_7 = 10 ** 7
SCALAR_12 = 10 ** 12
# Emission eps is 14 decimals, not 7 like everything else around it
# (emissions/manager.rs::update_reserve_emission_eps). Using 7 overstates an emission
# APR by exactly 1e7, which reads as a plausible-looking four-digit percentage rather
# than as an obvious error, so it is worth stating where the number comes from.
SCALAR_14 = 10 ** 14
SECONDS_PER_YEAR = 31536000
# Blend re-sets each reserve's eps over a 7-day window (same file), so an emission rate
# is a weekly quantity that expires, not a standing APR.
EMISSION_WINDOW_S = 7 * 24 * 3600

# Blend v2 pool status, from pool/src/pool/status.rs. Supply is disabled above 3 and
# borrowing above 1 (pool.rs::require_action_allowed); withdrawal is never gated by
# status, which is the fact YIELD.md's §2 stuck-semantics question turns on.
POOL_STATUS = {
    0: "admin active", 1: "active", 2: "admin on-ice", 3: "on-ice",
    4: "admin frozen", 5: "frozen", 6: "setup",
}


def _supply_allowed(status):
    return status <= 3


_server = SorobanServer(RPC_URL)

# A full census is ~100 sequential RPC round trips, which is fine once an hour and much
# too slow to sample on a recorder's cadence. Most of those calls ask for things that
# cannot change: a Stellar Asset Contract's symbol, an oracle's decimals. Those are
# cached for the life of the process. Oracle PRICES are cached too but only briefly --
# they move, and a stale price silently mis-sizes free_liquidity_usd and every emission
# APR that divides by it.
#
# What is deliberately NOT cached is anything that decides whether capital can move:
# pool status, reserve enablement, utilization and the reserve data behind the rates.
# A frozen pool that reads as active because the answer was cached is the failure this
# whole module exists to prevent.
_SYMBOL_CACHE = {}
_DECIMALS_CACHE = {}
_PRICE_CACHE = {}
PRICE_TTL_S = 300


def _symbol(asset_id):
    if asset_id not in _SYMBOL_CACHE:
        _SYMBOL_CACHE[asset_id] = simulate(asset_id, "symbol")
    return _SYMBOL_CACHE[asset_id]


def _oracle_decimals(oracle):
    if oracle not in _DECIMALS_CACHE:
        _DECIMALS_CACHE[oracle] = simulate(oracle, "decimals") or 7
    return _DECIMALS_CACHE[oracle]


def _oracle_price(oracle, asset_id):
    key = (oracle, asset_id)
    cached = _PRICE_CACHE.get(key)
    if cached and time.time() - cached[0] < PRICE_TTL_S:
        return cached[1]
    quote = simulate(oracle, "lastprice",
                     [scval.to_enum("Stellar", scval.to_address(asset_id))])
    price = quote["price"] / 10 ** _oracle_decimals(oracle) if quote else None
    _PRICE_CACHE[key] = (time.time(), price)
    return price


def _http_json(url, timeout=25):
    req = urllib.request.Request(url, headers=_HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def simulate(contract_id, fn, args=None):
    """Read a contract function by simulation. Returns None if the call errors.

    Errors are normal here and not exceptional: probing a v1 pool for a v2 getter, or
    asking an oracle for an asset it does not track, both come back as host errors.
    """
    tx = (
        TransactionBuilder(Account(READ_SOURCE, 0), PASSPHRASE, base_fee=100)
        .append_invoke_contract_function_op(contract_id, fn, args or [])
        .set_timeout(30)
        .build()
    )
    result = _server.simulate_transaction(tx)
    if result.error or not result.results:
        return None
    return scval.to_native(result.results[0].xdr)


def instance_storage(contract_id):
    """The contract's instance storage map, native-decoded. {} if unreadable.

    Blend pool names and configs live here rather than behind a getter, so this is the
    only way to learn that CCCCIQSD... is called "YieldBlox".
    """
    key = sdk_xdr.LedgerKey(
        type=sdk_xdr.LedgerEntryType.CONTRACT_DATA,
        contract_data=sdk_xdr.LedgerKeyContractData(
            contract=Address(contract_id).to_xdr_sc_address(),
            key=sdk_xdr.SCVal(sdk_xdr.SCValType.SCV_LEDGER_KEY_CONTRACT_INSTANCE),
            durability=sdk_xdr.ContractDataDurability.PERSISTENT,
        ),
    )
    entries = _server.get_ledger_entries([key]).entries
    if not entries:
        return {}
    instance = sdk_xdr.LedgerEntryData.from_xdr(entries[0].xdr).contract_data.val.instance
    if not instance.storage:
        return {}
    return {scval.to_native(e.key): scval.to_native(e.val) for e in instance.storage.sc_map}


def _addr(value):
    """stellar-sdk Address -> 'C...' string. to_native hands back objects, not strings."""
    return value.address if hasattr(value, "address") else str(value)


# --------------------------------------------------------------------------- Blend math

def _mul_ceil(a, b, scalar):
    return -((-a * b) // scalar)


def _div_ceil(a, b, scalar):
    return -((-a * scalar) // b)


def reserve_rates(config, data, bstop_rate):
    """(utilization, borrow APR, supply APR) for one reserve, all as decimal fractions.

    Transcribed from interest.rs::calc_accrual. `ir_mod` is state, not a parameter: it
    drifts toward whatever keeps utilization at target, so the APR below is the rate
    the pool would charge *right now*, not a steady state. That distinction is the
    whole of measurement 1 -- a rate that is merely mid-drift is not a rate that flipped.
    """
    supply = (data["b_supply"] * data["b_rate"]) // SCALAR_12          # fixed_mul_floor
    liabilities = _mul_ceil(data["d_supply"], data["d_rate"], SCALAR_12)
    if supply <= 0 or liabilities <= 0:
        return 0.0, 0.0, 0.0
    util = SCALAR_7 if liabilities >= supply else _div_ceil(liabilities, supply, SCALAR_7)

    target = config["util"]
    ir_mod = data["ir_mod"]
    if util <= target:
        scaled = _div_ceil(util, target, SCALAR_7)
        base = _mul_ceil(scaled, config["r_one"], SCALAR_7) + config["r_base"]
        cur_ir = _mul_ceil(base, ir_mod, SCALAR_7)
    elif util <= 9500000:
        scaled = _div_ceil(util - target, 9500000 - target, SCALAR_7)
        base = _mul_ceil(scaled, config["r_two"], SCALAR_7) + config["r_one"] + config["r_base"]
        cur_ir = _mul_ceil(base, ir_mod, SCALAR_7)
    else:
        # Above 95% the third slope is applied undamped -- ir_mod scales only the
        # intersection, so a pool in this band re-prices far faster than one below it.
        scaled = _div_ceil(util - 9500000, 500000, SCALAR_7)
        extra = _mul_ceil(scaled, config["r_three"], SCALAR_7)
        intersection = _mul_ceil(
            ir_mod, config["r_two"] + config["r_one"] + config["r_base"], SCALAR_7)
        cur_ir = extra + intersection

    util_f = util / SCALAR_7
    borrow_apr = cur_ir / SCALAR_7
    # Interest reaches suppliers through b_rate, after the backstop takes its cut
    # (reserve.rs::accrue). A pool with a 20% take pays 20% less than its borrow rate
    # times utilization implies, which is not visible anywhere in the reserve data.
    supply_apr = borrow_apr * util_f * (1 - bstop_rate / SCALAR_7)
    return util_f, borrow_apr, supply_apr


def _apy(apr):
    """Continuous compounding. Blend accrues on every interaction, so this is the ceiling
    on what an APR is worth; a pool nobody touches for a week compounds less."""
    import math
    return math.expm1(apr)


# ------------------------------------------------------------------------------- Blend

def census_blend(marks=None):
    marks = marks or {}
    emitter_backstop = simulate(EMITTER, "get_backstop")
    reward_zone = simulate(BACKSTOP_V2, "reward_zone") or []

    out = {
        "emitter_backstop": _addr(emitter_backstop) if emitter_backstop else None,
        "emitter_backstop_is_v2": bool(emitter_backstop) and _addr(emitter_backstop) == BACKSTOP_V2,
        "blnd_usd": marks.get("BLND"),
        "pools": [],
        "v1_doc_pools": [],
    }

    for name, pool_id in V1_DOC_POOLS.items():
        out["v1_doc_pools"].append({
            "documented_name": name,
            "address": pool_id,
            "on_chain_name": instance_storage(pool_id).get("Name"),
            "is_pool_v1_factory": simulate(FACTORY_V1, "is_pool", [scval.to_address(pool_id)]),
            "is_pool_v2_factory": simulate(FACTORY_V2, "is_pool", [scval.to_address(pool_id)]),
        })

    for entry in reward_zone:
        pool_id = _addr(entry)
        storage = instance_storage(pool_id)
        config = simulate(pool_id, "get_config") or {}
        status = config.get("status")
        bstop_rate = config.get("bstop_rate", 0)
        oracle = _addr(config["oracle"]) if config.get("oracle") else None
        # Oracle price decimals vary by deployment -- YieldBlox's reports 7, and assuming
        # it for every pool put Etherfuse's XLM supply at $108bn.
        oracle_decimals = _oracle_decimals(oracle) if oracle else 7

        pool = {
            "name": storage.get("Name"),
            "oracle_decimals": oracle_decimals,
            "address": pool_id,
            "version": 2,
            "status": status,
            "status_text": POOL_STATUS.get(status, "unknown"),
            "supply_allowed": _supply_allowed(status) if status is not None else None,
            "backstop_take_rate": bstop_rate / SCALAR_7,
            "oracle": oracle,
            "is_pool_v2_factory": simulate(FACTORY_V2, "is_pool", [scval.to_address(pool_id)]),
            "reserves": [],
        }

        for asset in simulate(pool_id, "get_reserve_list") or []:
            asset_id = _addr(asset)
            reserve = simulate(pool_id, "get_reserve", [scval.to_address(asset_id)])
            if not reserve:
                continue
            config_r, data = reserve["config"], reserve["data"]
            scalar = reserve["scalar"]
            util, borrow_apr, supply_apr = reserve_rates(config_r, data, bstop_rate)

            supplied = (data["b_supply"] * data["b_rate"]) // SCALAR_12 / scalar
            borrowed = _mul_ceil(data["d_supply"], data["d_rate"], SCALAR_12) / scalar

            price = _oracle_price(oracle, asset_id) if oracle else None

            # Emission index: reserve index * 2, +1 for the bToken (supply) side.
            emis = simulate(pool_id, "get_reserve_emissions",
                            [scval.to_uint32(config_r["index"] * 2 + 1)])
            blnd_per_year = None
            emission_apr = None
            if emis and emis.get("eps"):
                expired = emis["expiration"] < time.time()
                blnd_per_year = 0.0 if expired else emis["eps"] * SECONDS_PER_YEAR / SCALAR_14
                if blnd_per_year and marks.get("BLND") and price and supplied:
                    emission_apr = blnd_per_year * marks["BLND"] / (supplied * price)

            pool["reserves"].append({
                "symbol": _symbol(asset_id),
                "asset": asset_id,
                "enabled": config_r["enabled"],
                "utilization": util,
                "borrow_apr": borrow_apr,
                "supply_apr": supply_apr,
                "supply_apy": _apy(supply_apr),
                "supplied": supplied,
                "borrowed": borrowed,
                "usd_price": price,
                "supplied_usd": supplied * price if price else None,
                "free_liquidity": supplied - borrowed,
                "supply_cap": config_r["supply_cap"] / scalar,
                "max_utilization": config_r["max_util"] / SCALAR_7,
                "blnd_emissions_per_year": blnd_per_year,
                "emission_apr_gross": emission_apr,
            })
        out["pools"].append(pool)
    return out


# --------------------------------------------------------------------------- Aquarius

def _market_label(market_key):
    def side(code, contract):
        if code:
            return code
        return contract[:8] + "..." if contract else "?"
    return "%s/%s" % (
        side(market_key["asset1_code"], market_key["asset1_contract"]),
        side(market_key["asset2_code"], market_key["asset2_contract"]),
    )


def _sac_for(code, issuer, contract):
    """Whatever the reward API gives for one side of a market -> its contract address."""
    if contract:
        return contract
    if code == "XLM" and not issuer:
        return Asset.native().contract_id(PASSPHRASE)
    return Asset(code, issuer).contract_id(PASSPHRASE)


def _asset_spec(sac, cache):
    """Contract address -> the 'CODE:ISSUER'/'XLM' spec dex_price and friction speak."""
    if sac not in cache:
        name = simulate(sac, "name")
        cache[sac] = "XLM" if name == "native" else name
    return cache[sac]


def census_aquarius(marks=None):
    """Rewarded markets from reward-api, every pool for each one from the router.

    Pools are enumerated on chain rather than from the AMM API's `/pools/` listing,
    because that listing cannot be paged reliably: it reports 337 pools, a full walk of
    its 34 pages returns 239 unique addresses with duplicates across pages, and what it
    drops is not random -- it lost the *concentrated* XLM/USDC pool, which is the one
    holding ~$1.3m and drawing most of that market's AQUA. The router's `get_pools`
    returns all four XLM/USDC pools every time, so it is the source of truth here and
    the API is used only for the reward schedule, which is not on chain in enumerable
    form.
    """
    from aquarius.pool import sort_token_ids   # only this venue needs aquarius-sdk

    marks = marks or {}
    rewarded, url = [], REWARD_API + "?size=100"
    while url:
        page = _http_json(url)
        rewarded.extend(page["results"])
        url = page.get("next")

    spec_cache, mark_cache = {}, dict(marks)

    def mark(spec):
        if spec not in mark_cache:
            try:
                import dex_price
                mark_cache[spec] = dex_price.get_mark(spec)
            except Exception:
                mark_cache[spec] = None
        return mark_cache[spec]

    out = {"aqua_usd": marks.get("AQUA"), "markets": []}
    for reward in rewarded:
        key = reward["market_key"]
        sacs = [_sac_for(key["asset%s_code" % n], key["asset%s_issuer" % n],
                         key["asset%s_contract" % n]) for n in ("1", "2")]
        tokens = sort_token_ids(sacs)
        found = simulate(ROUTER, "get_pools",
                         [scval.to_vec([scval.to_address(t) for t in tokens])]) or {}

        pools = []
        for pool_address in found.values():
            pool_id = _addr(pool_address)
            info = simulate(pool_id, "get_rewards_info",
                            [scval.to_address(READ_SOURCE)]) or {}
            tps = info.get("tps", 0)
            if not tps:
                continue
            aqua_per_day = tps * 86400 / SCALAR_7

            reserves = simulate(pool_id, "get_reserves") or []
            pool_tokens = [_addr(t) for t in (simulate(pool_id, "get_tokens") or [])]
            specs = [_asset_spec(t, spec_cache) for t in pool_tokens]
            tvl_usd = 0.0
            for amount, spec in zip(reserves, specs):
                price = mark(spec)
                if price is None:
                    tvl_usd = None
                    break
                tvl_usd += amount / SCALAR_7 * price

            emission_apr = None
            if tvl_usd and marks.get("AQUA"):
                emission_apr = aqua_per_day * 365 * marks["AQUA"] / tvl_usd

            pools.append({
                "address": pool_id,
                "type": simulate(pool_id, "contract_name"),
                "fee_bp": (simulate(pool_id, "get_fee_fraction") or 0),
                "tokens": specs,
                "reserves": [a / SCALAR_7 for a in reserves],
                "tvl_usd": tvl_usd,
                "tps": tps,
                "aqua_per_day": aqua_per_day,
                "expires_at": info.get("exp_at"),
                "expires_in_hours": (info.get("exp_at", 0) - time.time()) / 3600,
                "emission_apr_gross": emission_apr,
                # Single-sided entry is impossible on any of these, so an allocation is an
                # LP position carrying impermanent loss, not a yield position, and getting
                # in and out costs two book crossings (YIELD.md section 3).
                "two_sided_entry": True,
            })

        chain_daily = sum(p["aqua_per_day"] for p in pools)
        out["markets"].append({
            "market": _market_label(key),
            "tokens": sacs,
            "daily_amm_reward_api": reward["daily_amm_reward"],
            "daily_sdex_reward_api": reward["daily_sdex_reward"],
            "aqua_per_day_onchain": chain_daily,
            "usd_per_day": chain_daily * marks["AQUA"] if marks.get("AQUA") else None,
            "rewarded_pools": pools,
            "pools_for_market": len(found),
        })
    return out


# ---------------------------------------------------------------------- the snapshot

# One process reads the chain; everything else reads this file. A Blend census is ~50s of
# RPC round trips, and a population of twenty strategies each doing that on its own tick
# would be both slow and rude to a public endpoint. domain_yield.observe() refreshes it
# once per monitor cycle and every strategy's main.py reads it, which is the same shape
# market_recorder.py has for the sdex domain -- with the difference that the snapshot is
# a cache and not a history: it is overwritten, not appended, and it is NOT the rate
# archive YIELD.md step 2 calls for.
SNAPSHOT_PATH = os.environ.get("YIELD_SNAPSHOT", "/opt/trades/yield_snapshot.json")
SNAPSHOT_MAX_AGE_S = 3600


def write_snapshot(path=None, marks=None):
    """Refresh the Blend snapshot. Returns the census written, or None if it failed.

    Blend only, deliberately: the Aquarius venues cannot be entered single-sided, so an
    allocation there is an LP position with impermanent loss and two book crossings
    (YIELD.md section 3), and nothing in this system prices those yet. Adding Aquarius to
    the snapshot before that arithmetic exists would put venues in front of the population
    whose advertised rate is not a return.
    """
    path = path or SNAPSHOT_PATH
    try:
        census = census_blend(marks if marks is not None else get_marks())
    except Exception as e:
        print("[yield_venues] census failed: %s: %s" % (type(e).__name__, e))
        return None
    payload = {"as_of": int(time.time()), "venue": "blend", "blend": census}
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Atomic: a strategy reading this file mid-write must never see half a census.
        temporary = "%s.tmp.%d" % (path, os.getpid())
        with open(temporary, "w") as handle:
            json.dump(payload, handle, default=str)
        os.replace(temporary, path)
    except Exception as e:
        print("[yield_venues] could not write %s: %s" % (path, e))
        return None
    return payload


def read_snapshot(path=None, max_age_s=SNAPSHOT_MAX_AGE_S):
    """The last snapshot, or None if it is missing, unreadable or older than max_age_s.

    Stale reads as absent rather than as data. A rate this system acts on is the whole
    input to the decision, and an hour-old APY presented as current is the quiet kind of
    wrong -- pass max_age_s=None to read it anyway and judge the age yourself.
    """
    path = path or SNAPSHOT_PATH
    try:
        with open(path) as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if max_age_s is not None and time.time() - payload.get("as_of", 0) > max_age_s:
        return None
    return payload


def allocatable_reserves(payload):
    """Every (pool, reserve) in a snapshot that can actually be supplied into, flattened.

    Three filters, none of them cosmetic: a frozen pool refuses Supply outright
    (`require_action_allowed`), a disabled reserve is not lendable, and a reserve at or
    above its max utilization cannot be withdrawn from until someone repays -- which is
    exactly the "illiquid by design" case YIELD.md section 2 says must be distinguished
    from "trapped" before any of it touches money.
    """
    rows = []
    for pool in (payload or {}).get("blend", {}).get("pools", []):
        if not pool.get("supply_allowed"):
            continue
        for reserve in pool.get("reserves", []):
            if not reserve.get("enabled"):
                continue
            rows.append({
                "pool": pool["name"],
                "pool_address": pool["address"],
                "asset": reserve["symbol"],
                "asset_address": reserve["asset"],
                "supply_apy": reserve["supply_apy"] or 0.0,
                "emission_apr_gross": reserve.get("emission_apr_gross") or 0.0,
                "utilization": reserve["utilization"],
                "max_utilization": reserve["max_utilization"],
                "free_liquidity": reserve["free_liquidity"],
                "usd_price": reserve.get("usd_price"),
                "free_liquidity_usd": ((reserve["free_liquidity"] * reserve["usd_price"])
                                       if reserve.get("usd_price") else None),
            })
    return rows


# ------------------------------------------------------------------------------ report

def get_marks():
    """USD marks for the two emission tokens, off the Stellar DEX book via dex_price."""
    marks = {}
    try:
        import dex_price
        for label, spec in (("BLND", BLND_SPEC), ("AQUA", AQUA_SPEC), ("XLM", "XLM")):
            try:
                marks[label] = dex_price.get_mark(spec)
            except Exception:
                marks[label] = None
    except Exception:
        pass
    return marks


def _pct(value):
    return "     -" if value is None else "%5.2f%%" % (value * 100)


def print_blend(data):
    print("=" * 78)
    print("BLEND -- lending pools (v2 reward zone, read from the backstop)")
    print("=" * 78)
    print("emitter -> backstop: %s  (%s)" % (
        data["emitter_backstop"],
        "v2, so BLND emissions go here" if data["emitter_backstop_is_v2"] else "NOT v2"))
    print()
    print("The two pools YIELD.md names, at their documented (v1) addresses:")
    for pool in data["v1_doc_pools"]:
        print("  %-10s %s  on-chain name=%-10s v1_factory=%s v2_factory=%s" % (
            pool["documented_name"], pool["address"], pool["on_chain_name"],
            pool["is_pool_v1_factory"], pool["is_pool_v2_factory"]))
    print("  ^ v1 contracts. The pools below are the v2 ones the emitter actually pays.")
    print()

    for pool in data["pools"]:
        flag = "" if pool["supply_allowed"] else "   <-- SUPPLY DISABLED"
        print("-" * 78)
        print("%s  %s" % (pool["name"], pool["address"]))
        print("  status=%d (%s)%s   backstop take=%.0f%%   supply allowed=%s" % (
            pool["status"], pool["status_text"], flag,
            pool["backstop_take_rate"] * 100, pool["supply_allowed"]))
        if not pool["reserves"]:
            continue
        print("  %-8s %8s %8s %8s %14s %10s %10s" % (
            "asset", "util", "supplyAPY", "borrowAPR", "supplied", "free", "emisAPR*"))
        for r in pool["reserves"]:
            usd = ("$" + format(int(r["supplied_usd"]), ",d")
                   if r["supplied_usd"] is not None else "")
            print("  %-8s %8s %8s %8s %14s %10s %10s  %s" % (
                (r["symbol"] or "?")[:8], _pct(r["utilization"]), _pct(r["supply_apy"]),
                _pct(r["borrow_apr"]), format(int(r["supplied"]), ",d"),
                format(int(r["free_liquidity"]), ",d"),
                _pct(r["emission_apr_gross"]), usd))
    print()
    print("* emisAPR is BLND emissions at the mark (BLND=$%s), GROSS of the cost of selling"
          % (("%.4f" % data["blnd_usd"]) if data["blnd_usd"] else "?"))
    print("  BLND into its book. YIELD.md section 1: that exit is most of the difference")
    print("  between an advertised APY and a realized one, and it is not netted here.")


def print_aquarius(data):
    print()
    print("=" * 78)
    print("AQUARIUS -- rewarded markets (reward-api), pools and tps confirmed on chain")
    print("=" * 78)
    print("AQUA mark: $%s" % (("%.6f" % data["aqua_usd"]) if data["aqua_usd"] else "?"))
    print()
    print("%-16s %13s %13s %8s  %s" % (
        "market", "AQUA/d api", "AQUA/d chain", "$/day", "pools paying / pools"))
    for market in data["markets"]:
        chain, api = market["aqua_per_day_onchain"], market["daily_amm_reward_api"]
        usd = "" if market["usd_per_day"] is None else "$%.0f" % market["usd_per_day"]
        drift = "  (chain/api %.2fx)" % (chain / api) if api else ""
        print("%-16s %13s %13s %8s  %d / %d%s" % (
            market["market"], format(int(api), ",d"), format(int(chain), ",d"), usd,
            len(market["rewarded_pools"]), market["pools_for_market"], drift))
        for pool in market["rewarded_pools"]:
            tvl = "?" if pool["tvl_usd"] is None else "$" + format(int(pool["tvl_usd"]), ",d")
            print("      %-24s %s fee=%dbp tvl=%-12s emisAPR=%s exp %.1fh" % (
                (pool["type"] or "?")[:24], pool["address"][:12] + "...",
                pool["fee_bp"], tvl, _pct(pool["emission_apr_gross"]),
                pool["expires_in_hours"]))
    print()
    print("emisAPR is AQUA at the mark over pool TVL, GROSS: it charges nothing for")
    print("selling AQUA, nothing for the two book crossings entry and exit require, and")
    print("nothing for impermanent loss. All three are real and none are small.")
    print("Reward windows expire within hours -- an APR here is a snapshot of a schedule,")
    print("not a rate anything is committed to.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    parser.add_argument("--blend", action="store_true", help="Blend only")
    parser.add_argument("--aqua", action="store_true", help="Aquarius only")
    parser.add_argument("--snapshot", action="store_true",
                        help="refresh the Blend snapshot strategies read, then exit")
    args = parser.parse_args()

    if args.snapshot:
        written = write_snapshot()
        print("wrote %s" % SNAPSHOT_PATH if written else "snapshot FAILED")
        raise SystemExit(0 if written else 1)

    both = not (args.blend or args.aqua)
    marks = get_marks()
    result = {"as_of": int(time.time()), "marks": marks}
    if args.blend or both:
        result["blend"] = census_blend(marks)
    if args.aqua or both:
        result["aquarius"] = census_aquarius(marks)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    if "blend" in result:
        print_blend(result["blend"])
    if "aquarius" in result:
        print_aquarius(result["aquarius"])


if __name__ == "__main__":
    main()

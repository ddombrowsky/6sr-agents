#!/usr/bin/env python3
"""Discover tradeable Stellar assets, and decide whether one is safe to trade.

This module is the safety layer that replaces a hand-curated whitelist. It exists
because of one fact about Stellar: **asset codes are not unique**. Anyone can issue an
asset with code `USDC` or `AQUA` from their own account, and impostors are live on
pubnet right now. Confirmed via stellar.expert while writing this:

    AQUA-GBNZILST…  191,603 trustlines,  rating 8.5, liquidity 7   <- the real one
    AQUA-GCWRD7DX…      96 trustlines,   rating 3.8, liquidity 0
    AQUA-GBYI5PYL…      47 trustlines,   rating 3.7, liquidity 0

An LLM picking assets by name, from training-data memory, would pick "AQUA" and have no
way to tell these apart. So a (code, issuer) pair is only ever admitted on *evidence*,
gathered from several independent places that an impersonator would have to compromise
simultaneously.

## How a verdict is reached

`verify_asset` gathers evidence from five independent sources and requires:

  * at least MIN_EVIDENCE_POINTS positive signals, AND
  * at least MIN_SOURCES sources to have actually answered, AND
  * zero hard vetoes.

The second condition is the one that is easy to get wrong. Every source failure scores
zero *and* counts as "not consulted", so a network outage can never manufacture a pass by
making the vetoes unevaluable. Degradation is always toward XLM-only, never toward
"trade an unverified asset". `verify_asset` never raises and never returns None; a
verdict is always a dict, and `ok` is False unless everything lined up.

## This is one of three gates, not the whole defense

  1. evidence     -- here, called by the revision LLM. Advisory: the model can skip it.
  2. admission    -- monitor.py, before a clone starts and again on every later clone.
                     A snapshot; an asset admitted Monday can be rugged Tuesday.
  3. enforcement  -- stellar_trader.py, inside every real trade. Cheap re-check.

None of the three can cover the others' failure mode, which is why all three exist.
"""
import json
import time
from pathlib import Path

import requests

import assets
import dex_price

_EXPERT = 'https://api.stellar.expert/explorer/public'
_HORIZON = 'https://horizon.stellar.org'
_TIMEOUT = 15
_TOML_TIMEOUT = 10
_TOML_MAX_BYTES = 500_000

_UNIVERSE_CACHE = Path(__file__).resolve().parent / '.asset_universe_cache.json'
_VERIFY_CACHE = Path(__file__).resolve().parent / '.asset_verify_cache.json'
_UNIVERSE_TTL = 6 * 3600
_VERIFY_TTL = 3600

MIN_EVIDENCE_POINTS = 3
MIN_SOURCES = 3

# --- veto thresholds -------------------------------------------------------------
MIN_TRUSTLINES = 200        # a real asset has holders; 96 and 47 were the impostors
MIN_AGE_DAYS = 90           # a fresh issuer has no track record to verify
MAX_SPREAD_PCT = 0.05       # wider than this is not a market
MIN_BID_DEPTH_USD = 25.0    # must be able to exit many times the per-trade cap.
                            # stellar_trader's MAX_TRADE_USD_NONBASE is 0.50, so this is
                            # 50x; stated here as a literal rather than imported so this
                            # module stays free of the live-trading import graph.
MIN_EXPERT_RATING = 7.0
MIN_EXPERT_LIQUIDITY = 5

# --- impersonation defense -------------------------------------------------------
# Famous codes whose issuer is pinned. This is NOT a whitelist of tradeable assets --
# it is a *negative* rule that closes the single highest-value attack: an LLM "knows"
# USDC is safe, so it writes code USDC with whatever issuer it was handed. Any asset
# using one of these codes with a different issuer is rejected outright, no matter how
# good its other evidence looks. Assets whose codes are absent here are judged purely
# on evidence, which is what keeps discovery open-ended.
_PINNED_ISSUERS = {
    'USDC': 'GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN',
    'EURC': 'GDHU6WRG4IEQXM5NZ4BMPKOXHW76MZM4Y2IEMFDVXBSDP6SJY4ITNPP2',
    'AQUA': 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
    'yXLM': 'GARDNV3Q7YGT4AKSDF25LT32YSCCW4EV22Y2TV3I2PU2MMXJTEDL5T55',
    'yUSDC': 'GDGTVWSM4MGS4T7Z6W4RPWOCHE2I6RDFCIFZGS3DOA63LWQTRNZNTTFF',
}


def _trustline_count(value):
    """Normalize stellar.expert's trustline count, which has two different shapes.

    The single-asset endpoint returns a dict ({'total', 'authorized', 'funded'}); the
    asset *list* endpoint returns a list of counters. Same field name, same API, two
    types -- so read it through here rather than at each call site.
    """
    if isinstance(value, dict):
        return value.get('total')
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value if isinstance(value, int) else None


def _cache_get(path, key, ttl):
    try:
        entry = json.loads(path.read_text()).get(key)
        if entry and time.time() - entry['ts'] < ttl:
            return entry['value']
    except Exception:
        pass
    return None


def _cache_put(path, key, value, ttl):
    try:
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except Exception:
                data = {}
        data[key] = {'ts': time.time(), 'value': value}
        cutoff = time.time() - ttl * 10
        data = {k: v for k, v in data.items() if v.get('ts', 0) > cutoff}
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except Exception:
        pass


# ------------------------------------------------------------------ sources

def _expert_asset(code, issuer):
    """stellar.expert's asset record: rating, trustlines, volume, age, SAC. None on
    failure. The single richest source, which is exactly why it cannot be the only one."""
    try:
        resp = requests.get(f'{_EXPERT}/asset/{code}-{issuer}', timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) and data.get('asset') else None
    except Exception:
        return None


def _horizon_asset(code, issuer):
    """Horizon's own view: issuer flags and authorized-holder count. Independent of
    stellar.expert -- different operator, different infrastructure.

    Note `amount` and `num_accounts` are null on current Horizon; the live fields are
    `accounts.authorized` and `balances.authorized`.
    """
    try:
        resp = requests.get(f'{_HORIZON}/assets',
                            params={'asset_code': code, 'asset_issuer': issuer},
                            timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None
        records = resp.json().get('_embedded', {}).get('records', [])
        return {'records': records, 'record': records[0] if records else None}
    except Exception:
        return None


def _issuer_account(issuer):
    try:
        resp = requests.get(f'{_HORIZON}/accounts/{issuer}', timeout=_TIMEOUT)
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None


def _toml_declares(home_domain, code, issuer):
    """Does the issuer's own domain publish a SEP-1 stellar.toml declaring this exact
    (code, issuer) pair? Fetched directly from the domain rather than read out of
    stellar.expert's `toml_info`, so it is a genuinely independent source: an attacker
    would need to control the real domain, not just look convincing on an explorer.

    Returns True/False, or None if the domain could not be reached at all (which counts
    as "not consulted", not as a failure).
    """
    if not home_domain:
        return False   # no domain to check is a real negative, not an outage
    try:
        resp = requests.get(
            f'https://{home_domain}/.well-known/stellar.toml',
            timeout=_TOML_TIMEOUT,
            headers={'User-Agent': 'stellar-strategy-bot/1.0'})
        if resp.status_code != 200:
            return False
        text = resp.text[:_TOML_MAX_BYTES]
    except Exception:
        return None

    # Deliberately not a TOML parse: the file only has to *contain* a currency entry
    # naming both this code and this issuer. Requiring both in the same [[CURRENCIES]]
    # block via string scanning is enough, and avoids depending on a TOML library that
    # isn't installed in the trading container.
    for block in text.split('[[CURRENCIES]]')[1:]:
        block = block.split('[[')[0]
        if f'"{code}"' in block or f"'{code}'" in block or f'= {code}' in block:
            if issuer in block:
                return True
    return False


def _hourly_activity(spec, hours=24):
    """How many of the last `hours` hourly buckets had trades. None if unreachable."""
    candles = dex_price.get_candles(spec, hours=hours)
    if not candles:
        return 0 if candles == [] else None
    return sum(1 for c in candles if c.get('trade_count', 0) > 0)


# ------------------------------------------------------------------ verdict

def verify_asset(code, issuer, refresh=False):
    """Gather evidence on (code, issuer) and return a verdict dict. Never raises.

        {'ok': bool, 'spec': str, 'score': int, 'sources_consulted': int,
         'evidence': {...}, 'vetoes': [str], 'checked_at': float}

    `ok` is True only with >= MIN_EVIDENCE_POINTS signals, >= MIN_SOURCES sources
    reachable, and no vetoes.
    """
    checked_at = time.time()
    try:
        spec = assets.canonical(code, issuer)
    except assets.AssetError as e:
        return {'ok': False, 'spec': f'{code}:{issuer}', 'score': 0,
                'sources_consulted': 0, 'evidence': {},
                'vetoes': [f'malformed asset identity: {e}'], 'checked_at': checked_at}

    if assets.is_native(spec):
        return {'ok': False, 'spec': spec, 'score': 0, 'sources_consulted': 0,
                'evidence': {},
                'vetoes': ['XLM is the permanent base leg, not a selectable extra asset'],
                'checked_at': checked_at}

    if not refresh:
        cached = _cache_get(_VERIFY_CACHE, spec, _VERIFY_TTL)
        if cached is not None:
            return cached

    evidence, vetoes = {}, []
    points = sources = 0

    # Pinned-issuer check runs before any network call: it needs no evidence and no
    # amount of evidence can overturn it.
    pinned = _PINNED_ISSUERS.get(code.upper()) or _PINNED_ISSUERS.get(code)
    if pinned and pinned != issuer:
        vetoes.append(
            f'code {code} is pinned to issuer {pinned[:8]}… but this asset is issued by '
            f'{issuer[:8]}… -- impersonation of a well-known asset')

    # --- source 1: stellar.expert
    expert = _expert_asset(code, issuer)
    if expert is not None:
        sources += 1
        rating = expert.get('rating') or {}
        total_tl = _trustline_count(expert.get('trustlines'))
        created = expert.get('created')
        age_days = (checked_at - created) / 86400 if created else None

        evidence['expert_rating_avg'] = rating.get('average')
        evidence['expert_rating_liquidity'] = rating.get('liquidity')
        evidence['trustlines'] = total_tl
        evidence['volume7d'] = expert.get('volume7d')
        evidence['age_days'] = round(age_days, 1) if age_days is not None else None
        evidence['home_domain'] = expert.get('home_domain')
        evidence['contract'] = expert.get('contract')

        if (rating.get('average') or 0) >= MIN_EXPERT_RATING and \
           (rating.get('liquidity') or 0) >= MIN_EXPERT_LIQUIDITY and \
           (expert.get('volume7d') or 0) > 0:
            points += 1

        if total_tl is not None and total_tl < MIN_TRUSTLINES:
            vetoes.append(f'only {total_tl} trustlines (need >= {MIN_TRUSTLINES})')
        if age_days is not None and age_days < MIN_AGE_DAYS:
            vetoes.append(f'issued {age_days:.0f} days ago (need >= {MIN_AGE_DAYS})')
        if not expert.get('volume7d'):
            vetoes.append('no measurable 7d volume')

    # --- source 2: Horizon /assets
    horizon = _horizon_asset(code, issuer)
    if horizon is not None:
        sources += 1
        record = horizon['record']
        if record is None:
            vetoes.append('Horizon knows no such (code, issuer) asset')
        else:
            flags = record.get('flags') or {}
            authorized = (record.get('accounts') or {}).get('authorized')
            evidence['horizon_authorized'] = authorized
            evidence['flags'] = flags

            if authorized is not None and authorized >= 1000:
                points += 1

            # Clawback lets the issuer take tokens you already hold. Irreversible total
            # loss with no action available to us, so it is a hard veto regardless of
            # how good everything else looks.
            if flags.get('auth_clawback_enabled'):
                vetoes.append('issuer has clawback enabled -- can reclaim the asset')

            # auth_required means a trustline must be approved by the issuer before it
            # can hold anything. ensure_trustline would create a trustline that can
            # never receive the asset, so this is a functional blocker, not a risk
            # judgement.
            if flags.get('auth_required'):
                vetoes.append('issuer requires authorization to hold')

            # auth_revocable deliberately is NOT a veto. Verified live: the real
            # Circle USDC (GA5ZSEJY…) has auth_revocable = true, as do most regulated
            # stablecoins -- it is how they meet sanctions obligations. Vetoing it would
            # exclude every such asset, including the settlement asset the live path
            # already uses. The risk it represents (a frozen, unsellable leg) is real
            # but is exactly what stellar_trader's stuck-position handling and
            # MAX_STUCK_USD kill-switch exist for. Recorded so it is visible, and it
            # withholds an evidence point rather than ending the assessment.
            if flags.get('auth_revocable'):
                evidence['auth_revocable'] = True
            elif authorized is not None and authorized >= 1000:
                points += 1

    # --- source 3: SEP-1 stellar.toml on the issuer's own domain
    home_domain = evidence.get('home_domain')
    if home_domain is None:
        account = _issuer_account(issuer)
        if account is not None:
            home_domain = account.get('home_domain')
            evidence['home_domain'] = home_domain
    toml_match = _toml_declares(home_domain, code, issuer)
    evidence['toml_match'] = toml_match
    if toml_match is not None:
        sources += 1
        if toml_match:
            points += 1
        # A missing or non-declaring stellar.toml withholds a point but is NOT a veto.
        # Verified live: circle.com/.well-known/stellar.toml 404s, so the canonical
        # Circle USDC -- one of the most liquid, least controversial assets on the
        # network -- publishes no SEP-1 toml at its home_domain. A veto here would
        # reject it while doing nothing extra against impostors, which already fail on
        # trustlines, volume, liquidity and the pinned-issuer rule. Absent evidence is
        # not the same as disqualifying evidence.

    # --- source 4: live order book
    book = dex_price.get_orderbook(spec)
    if book is not None:
        sources += 1
        evidence['spread_pct'] = round(book['spread_pct'], 6)
        evidence['bid_depth_usd'] = round(book['bid_depth_usd'], 2)
        evidence['ask_depth_usd'] = round(book['ask_depth_usd'], 2)
        if book['spread_pct'] <= MAX_SPREAD_PCT and book['bid_depth_usd'] >= MIN_BID_DEPTH_USD:
            points += 1
        if book['spread_pct'] > MAX_SPREAD_PCT:
            vetoes.append(f'spread {book["spread_pct"]:.2%} exceeds {MAX_SPREAD_PCT:.0%}')
        if book['bid_depth_usd'] < MIN_BID_DEPTH_USD:
            vetoes.append(
                f'bid depth ${book["bid_depth_usd"]:.2f} below ${MIN_BID_DEPTH_USD:.2f} '
                f'-- cannot exit a position')
    else:
        vetoes.append('no order book against USDC')

    # --- source 5: sustained trading activity
    active_buckets = _hourly_activity(spec)
    if active_buckets is not None:
        sources += 1
        evidence['active_hourly_buckets'] = active_buckets
        if active_buckets >= 18:
            points += 1
        elif active_buckets == 0:
            vetoes.append('no trades in the last 24h')

    ok = (not vetoes) and points >= MIN_EVIDENCE_POINTS and sources >= MIN_SOURCES
    if sources < MIN_SOURCES:
        vetoes.append(
            f'only {sources} of 5 evidence sources reachable (need >= {MIN_SOURCES}); '
            f'refusing to admit an asset that could not be checked')

    verdict = {'ok': ok, 'spec': spec, 'score': points, 'sources_consulted': sources,
               'evidence': evidence, 'vetoes': vetoes, 'checked_at': checked_at}
    _cache_put(_VERIFY_CACHE, spec, verdict, _VERIFY_TTL)
    return verdict


def asset_summary(code, issuer):
    """Facts about an asset with no verdict attached -- for the revision LLM to read.

    Separate from verify_asset on purpose: the model should see the underlying numbers
    and form its own view, rather than only ever being told yes/no by a gate it can't
    inspect.
    """
    verdict = verify_asset(code, issuer)
    return {'spec': verdict['spec'], 'evidence': verdict['evidence'],
            'would_be_admitted': verdict['ok'], 'concerns': verdict['vetoes']}


# ------------------------------------------------------------------ discovery

def discover_candidates(limit=15, refresh=False):
    """Rank plausible extra assets from live network data. [] on failure.

    Ordering is by stellar.expert's own composite rating, then trustline count. This
    only *proposes*; nothing here is admitted without passing verify_asset, and callers
    must not treat presence in this list as approval.
    """
    if not refresh:
        cached = _cache_get(_UNIVERSE_CACHE, 'universe', _UNIVERSE_TTL)
        if cached is not None:
            return cached[:limit]

    try:
        resp = requests.get(f'{_EXPERT}/asset',
                            params={'sort': 'rating', 'order': 'desc', 'limit': 50},
                            timeout=_TIMEOUT)
        if resp.status_code != 200:
            return []
        records = resp.json().get('_embedded', {}).get('records', [])
    except Exception as e:
        print(f'[asset_discovery] universe fetch failed: {e}')
        return []

    out = []
    for r in records:
        name = r.get('asset') or ''
        parts = name.split('-')
        if len(parts) < 2:
            continue          # 'XLM' has no issuer; not a selectable extra asset
        code, issuer = parts[0], parts[1]
        if not assets.is_valid_code(code) or not assets.is_valid_issuer(issuer):
            continue
        if assets.is_reserved_code(code):
            continue
        rating = r.get('rating') or {}
        out.append({
            'code': code,
            'issuer': issuer,
            'spec': f'{code}:{issuer}',
            'rating_avg': rating.get('average'),
            'rating_liquidity': rating.get('liquidity'),
            'trustlines': _trustline_count(r.get('trustlines')),
            'volume7d': r.get('volume7d'),
            'domain': r.get('home_domain'),
        })

    out.sort(key=lambda a: ((a['rating_avg'] or 0), (a['trustlines'] or 0)), reverse=True)
    _cache_put(_UNIVERSE_CACHE, 'universe', out, _UNIVERSE_TTL)
    return out[:limit]


def cached_universe():
    """Whatever discover_candidates last found, without any network call."""
    return _cache_get(_UNIVERSE_CACHE, 'universe', _UNIVERSE_TTL) or []


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3:
        print(json.dumps(verify_asset(sys.argv[1], sys.argv[2]), indent=2))
    else:
        print(f'{"CODE":<8} {"RATING":>6} {"LIQ":>4} {"TRUSTLINES":>11}  DOMAIN')
        for a in discover_candidates():
            print(f'{a["code"]:<8} {a["rating_avg"] or 0:>6.1f} '
                  f'{a["rating_liquidity"] or 0:>4} {a["trustlines"] or 0:>11,}  '
                  f'{a["domain"] or ""}')

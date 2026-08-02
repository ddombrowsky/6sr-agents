#!/usr/bin/env python3
"""Canonical Stellar asset identity. The one place an asset is defined.

Every other module imports from here rather than passing bare asset codes around,
because **a Stellar asset code is not unique**: anyone can issue an asset with code
`USDC` or `AQUA` from their own account, and impostors exist on pubnet right now. An
asset is the *pair* (code, issuer), and this module exists to make it structurally
awkward to represent one without the other.

The canonical string form -- what gets stored in config.json, state.json, trade logs and
the verified-asset registry -- is:

    'XLM'                    the native asset, the only one with no issuer
    'AQUA:GBNZILST...4M67AQUA'   any credit asset, code and issuer inseparable

Deliberately zero network access and zero dependencies, so it can be imported from
anywhere (including the price feed and the live trader) without an import cycle or a
timeout. Nothing here validates that an asset is *safe* or *tradeable* -- that is
asset_discovery.verify_asset's job. This module only answers "is this a well-formed
asset identity, and what is its canonical name".
"""

NATIVE = 'XLM'

# Circle's well-known pubnet USDC, the quote asset for the DEX price feed and the
# settlement asset for live trading. Named here so the rest of the system has one
# spelling of it; stellar_trader.py's own copy is the live-trading boundary's.
USDC_ISSUER = 'GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN'

# A Stellar account/issuer address: 'G' + 55 base32 characters (Ed25519 public key,
# StrKey-encoded). Not a checksum validation -- that would need base32 decoding and a
# CRC16 -- just a shape check that rejects the realistic failure mode, which is an LLM
# writing a truncated, elided ('GBNZ...'), or entirely invented address.
_ISSUER_LEN = 56
_ISSUER_PREFIX = 'G'
_BASE32_ALPHABET = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567')

# SEP-11 permits alphanum4 and alphanum12 asset codes.
_MAX_CODE_LEN = 12

# Codes that may never be paired with an issuer, in any casing. 'XLM' is the native
# asset and cannot be issued; a credit asset calling itself XLM (or NATIVE) is either a
# mistake or an impersonation attempt, and there is no legitimate reason to hold one.
_RESERVED_CODES = frozenset({'XLM', 'NATIVE'})


class AssetError(ValueError):
    """Malformed asset identity. Raised rather than returned so a bad asset can never
    silently degrade into a differently-named valid one."""


def is_valid_code(code):
    """True if `code` is a well-formed SEP-11 asset code (1-12 alphanumerics)."""
    return (
        isinstance(code, str)
        and 1 <= len(code) <= _MAX_CODE_LEN
        and code.isalnum()
        and code.isascii()
    )


def is_valid_issuer(issuer):
    """True if `issuer` has the shape of a Stellar account address."""
    return (
        isinstance(issuer, str)
        and len(issuer) == _ISSUER_LEN
        and issuer[0] == _ISSUER_PREFIX
        and set(issuer[1:]) <= _BASE32_ALPHABET
    )


def is_reserved_code(code):
    """True if `code` is one no credit asset may use (XLM/NATIVE, any casing)."""
    return isinstance(code, str) and code.upper() in _RESERVED_CODES


def canonical(code, issuer=None):
    """Return the canonical spec string for an asset, or raise AssetError.

    canonical('XLM')            -> 'XLM'
    canonical('XLM', None)      -> 'XLM'
    canonical('AQUA', 'GBNZ..') -> 'AQUA:GBNZ..'
    canonical('XLM', 'GBNZ..')  -> raises

    That last case is the whole point of routing asset identity through one function:
    the native asset has no issuer, so a credit asset with code XLM is never
    representable and can never be confused with the base leg downstream.
    """
    if not is_valid_code(code):
        raise AssetError(f'malformed asset code: {code!r}')

    if issuer is None:
        if is_reserved_code(code):
            return NATIVE
        raise AssetError(
            f'asset {code!r} has no issuer; a credit asset is the pair (code, issuer) '
            f'and the code alone is not a usable identity'
        )

    if is_reserved_code(code):
        raise AssetError(
            f'{code!r} is the native asset and cannot have an issuer (got {issuer!r}); '
            f'a credit asset claiming this code is an impersonation'
        )
    if not is_valid_issuer(issuer):
        raise AssetError(
            f'malformed issuer for {code!r}: {issuer!r} (expected a 56-character '
            f'G... address; never abbreviate or recall one from memory)'
        )
    return f'{code}:{issuer}'


def parse(spec):
    """Inverse of canonical. Returns (code, issuer), issuer None for native.

    Accepts the canonical string, a bare 'XLM', or a dict with 'code'/'issuer' keys
    (the shape assets take in config.json), so callers can hand through whatever the
    config gave them without normalizing first. Raises AssetError on anything else.
    """
    if isinstance(spec, dict):
        return parse(canonical(spec.get('code'), spec.get('issuer')))

    if not isinstance(spec, str):
        raise AssetError(f'not an asset spec: {spec!r}')

    if ':' not in spec:
        if is_reserved_code(spec):
            return NATIVE, None
        raise AssetError(
            f'{spec!r} is a bare asset code with no issuer; use CODE:ISSUER'
        )

    code, _, issuer = spec.partition(':')
    canonical(code, issuer)  # re-validate; raises on anything malformed
    return code, issuer


def is_native(spec):
    """True if `spec` names the native asset. Never raises for well-formed input."""
    code, issuer = parse(spec)
    return issuer is None and code == NATIVE


def normalize(spec):
    """Canonicalize any accepted spec form into the canonical string."""
    code, issuer = parse(spec)
    return canonical(code, issuer)


def sep11(spec):
    """SEP-11 form for the `stellar` CLI: 'native' or 'CODE:ISSUER'."""
    code, issuer = parse(spec)
    return 'native' if issuer is None else f'{code}:{issuer}'


def horizon_params(spec, prefix):
    """Horizon query parameters naming this asset, e.g. for /order_book or /paths.

    `prefix` is the Horizon parameter family: 'selling_asset', 'buying_asset',
    'base', 'counter', 'source_asset', 'destination_asset'. Horizon spells the native
    asset as a lone `<prefix>_type=native` and a credit asset as a type/code/issuer
    triple, which is fiddly enough to get wrong by hand at each call site.
    """
    code, issuer = parse(spec)
    if issuer is None:
        return {f'{prefix}_type': 'native'}
    asset_type = 'credit_alphanum4' if len(code) <= 4 else 'credit_alphanum12'
    return {
        f'{prefix}_type': asset_type,
        f'{prefix}_code': code,
        f'{prefix}_issuer': issuer,
    }


def usdc():
    """Canonical spec for the USDC quote asset."""
    return canonical('USDC', USDC_ISSUER)


def display(spec, issuer_chars=6):
    """Short human-readable form for logs and tables: 'AQUA:GBNZIL…'.

    For display only -- never write this to config.json, state.json or any registry.
    An abbreviated issuer is exactly what an impersonation looks like, so the
    truncation must not survive into anything that gets read back.
    """
    code, issuer = parse(spec)
    if issuer is None:
        return code
    return f'{code}:{issuer[:issuer_chars]}…'

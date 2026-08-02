#!/usr/bin/env python3
"""Standalone self-test for assets.py and portfolio.py.

There is no test runner in this repo (no pytest, no CI), so this is a plain script:

    python3 /opt/tools/selftest_portfolio.py

Exits 0 and prints a pass count, or exits 1 on the first failure with the offending
values. Run it after touching either module -- both sit underneath every strategy and
the scoring loop, so a regression here is silent everywhere else.

The state.json fixtures are copies of real files from /opt/strategies as of the
multi-asset migration, including the awkward ones (a fully-invested strategy, and one
carrying ~50 private keys of its own).
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import assets
import portfolio

# Both are real pubnet issuers of an asset with code AQUA, confirmed via
# stellar.expert: the first has 191,603 trustlines and a liquidity rating of 7; the
# second has 96 trustlines and a liquidity rating of 0. They exist simultaneously on
# chain, which is the entire reason a bare asset code is not a usable identity.
_AQUA = 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA'
_IMPOSTOR = 'GCWRD7DXFTA3TDUIHXRLUBY4RNJCEHUYJ5LQB3QWNYITSXIRDBGRGT32'
_USDC = assets.USDC_ISSUER

# Real files from /opt/strategies, verbatim.
_V1_CASH = {'balance_usd': 1013.4486359716716, 'balance_xlm': 0.0}
_V1_INVESTED = {'balance_usd': 0.0, 'balance_xlm': 5824.719188789025}
_V1_PRIVATE = {
    'balance_usd': 1000.0, 'balance_xlm': 0.0,
    'price_history': [0.171397, 0.171412, 0.171401],
    'prev_short_sma': None, 'prev_long_sma': None, 'last_buy_price': None,
}

_passed = 0
_failures = []


def check(label, condition, detail=''):
    global _passed
    if condition:
        _passed += 1
    else:
        _failures.append(f'{label}{": " + detail if detail else ""}')


def check_raises(label, exc, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
    except exc:
        check(label, True)
        return
    except Exception as e:  # wrong exception type is still a failure
        check(label, False, f'raised {type(e).__name__} instead of {exc.__name__}')
        return
    check(label, False, f'returned {result!r} instead of raising')


# --------------------------------------------------------------------------- assets

check('canonical native', assets.canonical('XLM') == 'XLM')
check('canonical native explicit None', assets.canonical('XLM', None) == 'XLM')
check('canonical credit', assets.canonical('AQUA', _AQUA) == f'AQUA:{_AQUA}')

# The load-bearing rule: a credit asset can never claim to be XLM, and a bare code is
# never a usable identity.
check_raises('XLM with issuer rejected', assets.AssetError,
             assets.canonical, 'XLM', _AQUA)
check_raises('native lowercase with issuer rejected', assets.AssetError,
             assets.canonical, 'xlm', _AQUA)
check_raises('NATIVE with issuer rejected', assets.AssetError,
             assets.canonical, 'NATIVE', _AQUA)
check_raises('bare code rejected', assets.AssetError, assets.canonical, 'AQUA')
check_raises('bare code parse rejected', assets.AssetError, assets.parse, 'AQUA')

# Abbreviated / invented issuers are the realistic LLM failure mode.
check_raises('elided issuer rejected', assets.AssetError,
             assets.canonical, 'AQUA', 'GBNZ...')
check_raises('short issuer rejected', assets.AssetError,
             assets.canonical, 'AQUA', 'GBNZILST')
check_raises('non-G issuer rejected', assets.AssetError,
             assets.canonical, 'AQUA', 'A' + _AQUA[1:])
check_raises('lowercase issuer rejected', assets.AssetError,
             assets.canonical, 'AQUA', _AQUA.lower())
check_raises('issuer with 0/1/8/9 rejected', assets.AssetError,
             assets.canonical, 'AQUA', 'G' + '0' * 55)
check_raises('empty code rejected', assets.AssetError, assets.canonical, '', _AQUA)
check_raises('13-char code rejected', assets.AssetError,
             assets.canonical, 'A' * 13, _AQUA)
check_raises('non-alnum code rejected', assets.AssetError,
             assets.canonical, 'AQ-UA', _AQUA)

check('parse native', assets.parse('XLM') == ('XLM', None))
check('parse credit', assets.parse(f'AQUA:{_AQUA}') == ('AQUA', _AQUA))
check('parse dict', assets.parse({'code': 'AQUA', 'issuer': _AQUA}) == ('AQUA', _AQUA))
check('parse dict native', assets.parse({'code': 'XLM', 'issuer': None}) == ('XLM', None))
check('is_native true', assets.is_native('XLM'))
check('is_native false', not assets.is_native(f'AQUA:{_AQUA}'))
check('normalize idempotent',
      assets.normalize(assets.normalize(f'AQUA:{_AQUA}')) == f'AQUA:{_AQUA}')

check('sep11 native', assets.sep11('XLM') == 'native')
check('sep11 credit', assets.sep11(f'AQUA:{_AQUA}') == f'AQUA:{_AQUA}')

check('horizon native', assets.horizon_params('XLM', 'selling_asset')
      == {'selling_asset_type': 'native'})
check('horizon alphanum4', assets.horizon_params(f'AQUA:{_AQUA}', 'buying_asset') == {
    'buying_asset_type': 'credit_alphanum4',
    'buying_asset_code': 'AQUA', 'buying_asset_issuer': _AQUA})
check('horizon alphanum12',
      assets.horizon_params(f'LONGCODE1234:{_AQUA}', 'base')['base_type']
      == 'credit_alphanum12')

check('usdc helper', assets.usdc() == f'USDC:{_USDC}')
# Real and impostor AQUA must never collide as identities.
check('impostor distinct from real',
      assets.canonical('AQUA', _AQUA) != assets.canonical('AQUA', _IMPOSTOR))
check('display truncates', assets.display(f'AQUA:{_AQUA}') == 'AQUA:GBNZIL…')
check('display native', assets.display('XLM') == 'XLM')

# ------------------------------------------------------------------------ normalize

s = portfolio.normalize_state(copy.deepcopy(_V1_CASH))
check('v1 cash -> schema 2', s['schema_version'] == 2)
check('v1 cash usd preserved', s['balance_usd'] == 1013.4486359716716)
check('v1 cash xlm mirrored', s['balance_xlm'] == 0.0)
check('v1 cash has XLM leg', s['positions']['XLM']
      == {'code': 'XLM', 'issuer': None, 'amount': 0.0})

s = portfolio.normalize_state(copy.deepcopy(_V1_INVESTED))
check('v1 invested seeds position from balance_xlm',
      s['positions']['XLM']['amount'] == 5824.719188789025)
check('v1 invested mirror agrees', s['balance_xlm'] == s['positions']['XLM']['amount'])

# Idempotency: normalizing twice must be identical to normalizing once, or every
# execute_trade call would drift the state a little further each tick.
once = portfolio.normalize_state(copy.deepcopy(_V1_INVESTED))
twice = portfolio.normalize_state(copy.deepcopy(once))
check('normalize idempotent', once == twice, f'{once} != {twice}')

# Private strategy keys must survive -- several running strategies keep their whole
# indicator state in state.json.
s = portfolio.normalize_state(copy.deepcopy(_V1_PRIVATE))
check('private keys preserved', s['price_history'] == [0.171397, 0.171412, 0.171401])
check('private None keys preserved',
      s['prev_short_sma'] is None and 'last_buy_price' in s)

# Garbage in, usable state out -- normalize_state is called inside a trading loop and
# inside monitor's scoring pass, and must never raise.
for label, garbage in [('None', None), ('list', []), ('string', 'nope'),
                       ('empty', {}), ('nan usd', {'balance_usd': float('nan')}),
                       ('inf xlm', {'balance_xlm': float('inf')}),
                       ('str usd', {'balance_usd': 'abc'}),
                       ('positions not dict', {'positions': 'x'}),
                       ('position not dict', {'positions': {'XLM': 5}})]:
    try:
        g = portfolio.normalize_state(garbage)
        ok = (g['schema_version'] == 2 and 'XLM' in g['positions']
              and isinstance(g['balance_usd'], float)
              and g['balance_usd'] == g['balance_usd'])
    except Exception as e:
        ok = False
        g = f'raised {type(e).__name__}: {e}'
    check(f'garbage tolerated ({label})', ok, repr(g))

# A position whose dict key disagrees with its own code/issuer: the entry wins and the
# key is re-derived, so a hand-edited state can't smuggle a position under a false name.
s = portfolio.normalize_state({
    'balance_usd': 10.0,
    'positions': {'TOTALLY_WRONG': {'code': 'AQUA', 'issuer': _AQUA, 'amount': 5.0}}})
check('mismatched key re-derived', f'AQUA:{_AQUA}' in s['positions'])
check('mismatched key dropped', 'TOTALLY_WRONG' not in s['positions'])

# An unidentifiable position is dropped rather than guessed at.
s = portfolio.normalize_state({
    'balance_usd': 10.0,
    'positions': {'JUNK': {'code': 'JUNK', 'issuer': 'nope', 'amount': 5.0}}})
check('unidentifiable position dropped', list(s['positions']) == ['XLM'])

# positions wins over balance_xlm when they disagree (documented authority rule).
s = portfolio.normalize_state({
    'balance_usd': 0.0, 'balance_xlm': 999.0,
    'positions': {'XLM': {'code': 'XLM', 'issuer': None, 'amount': 42.0}}})
check('positions authoritative over balance_xlm', s['balance_xlm'] == 42.0)

# ------------------------------------------------------------------- get/add amounts

s = portfolio.normalize_state(copy.deepcopy(_V1_CASH))
check('get_amount native zero', portfolio.get_amount(s, 'XLM') == 0.0)
check('get_amount absent asset zero', portfolio.get_amount(s, f'AQUA:{_AQUA}') == 0.0)
check('get_amount malformed zero', portfolio.get_amount(s, 'AQUA') == 0.0)

portfolio.add_amount(s, 'XLM', 100.0)
check('add native', portfolio.get_amount(s, 'XLM') == 100.0)
check('add native mirrors', s['balance_xlm'] == 100.0)
portfolio.add_amount(s, 'XLM', -30.0)
check('subtract native', portfolio.get_amount(s, 'XLM') == 70.0)
check('subtract native mirrors', s['balance_xlm'] == 70.0)
portfolio.add_amount(s, 'XLM', -1e9)
check('native clamps at zero', portfolio.get_amount(s, 'XLM') == 0.0)
check('clamped native mirrors', s['balance_xlm'] == 0.0)

portfolio.add_amount(s, f'AQUA:{_AQUA}', 1234.5)
check('add credit creates leg', portfolio.get_amount(s, f'AQUA:{_AQUA}') == 1234.5)
check('credit leg has issuer', s['positions'][f'AQUA:{_AQUA}']['issuer'] == _AQUA)
check('credit leg does not touch balance_xlm', s['balance_xlm'] == 0.0)
# Real and impostor are separate legs, never merged.
portfolio.add_amount(s, f'AQUA:{_IMPOSTOR}', 7.0)
check('impostor is a separate leg',
      portfolio.get_amount(s, f'AQUA:{_AQUA}') == 1234.5
      and portfolio.get_amount(s, f'AQUA:{_IMPOSTOR}') == 7.0)
check_raises('add_amount rejects malformed', assets.AssetError,
             portfolio.add_amount, s, 'AQUA', 1.0)

# -------------------------------------------------------------------------- net worth

s = portfolio.normalize_state({'balance_usd': 100.0, 'balance_xlm': 1000.0})
portfolio.add_amount(s, f'AQUA:{_AQUA}', 2000.0)

nw, unpriced = portfolio.net_worth(s, {'XLM': 0.17, f'AQUA:{_AQUA}': 0.0003})
check('net worth sums legs', abs(nw - (100.0 + 170.0 + 0.6)) < 1e-9, str(nw))
check('net worth nothing unpriced', unpriced == [])

nw, unpriced = portfolio.net_worth(s, {'XLM': 0.17})
check('unpriced leg contributes zero', abs(nw - 270.0) < 1e-9, str(nw))
check('unpriced leg reported', unpriced == [f'AQUA:{_AQUA}'])

nw, unpriced = portfolio.net_worth(s, {'XLM': 0.17, f'AQUA:{_AQUA}': 0.0})
check('zero mark counts as unpriced', unpriced == [f'AQUA:{_AQUA}'])
nw, unpriced = portfolio.net_worth(s, {'XLM': 0.17, f'AQUA:{_AQUA}': None})
check('None mark counts as unpriced', unpriced == [f'AQUA:{_AQUA}'])
nw, _ = portfolio.net_worth(s, {})
check('no marks at all -> just cash', abs(nw - 100.0) < 1e-9, str(nw))

# A zero-balance leg is not "unpriced" -- it needs no mark.
s2 = portfolio.normalize_state({'balance_usd': 5.0, 'balance_xlm': 0.0})
nw, unpriced = portfolio.net_worth(s2, {})
check('zero balance leg needs no mark', unpriced == [] and nw == 5.0)

check('held_specs positive only', set(portfolio.held_specs(s))
      == {'XLM', f'AQUA:{_AQUA}'})
check('held_specs excluding native', portfolio.held_specs(s, include_native=False)
      == [f'AQUA:{_AQUA}'])

# ---------------------------------------------------------------- assets_from_config

# Every pre-multi-asset config is a valid config with an implicit empty asset list.
legacy_cfg = {'name': 'clone_x', 'buy_below': 0.147, 'sell_above': 0.224,
              'trade_amount_usd': 20.0}
check('legacy config -> no extra assets', portfolio.assets_from_config(legacy_cfg) == [])
check('legacy config declared specs', portfolio.declared_specs(legacy_cfg) == {'XLM'})

cfg = dict(legacy_cfg, assets=[
    {'code': 'AQUA', 'issuer': _AQUA, 'buy_below': 0.00033, 'sell_above': 0.000345,
     'trade_amount_usd': 2.0}])
got = portfolio.assets_from_config(cfg)
check('one valid asset', len(got) == 1)
check('asset spec built', got[0]['spec'] == f'AQUA:{_AQUA}')
check('asset thresholds carried', got[0]['buy_below'] == 0.00033)
check('declared specs includes native',
      portfolio.declared_specs(cfg) == {'XLM', f'AQUA:{_AQUA}'})

# Missing per-leg size falls back to the strategy's top-level size.
got = portfolio.assets_from_config(
    dict(legacy_cfg, assets=[{'code': 'AQUA', 'issuer': _AQUA}]))
check('asset size defaults to config', got[0]['trade_amount_usd'] == 20.0)

# The cap is enforced here, not only in monitor.
got = portfolio.assets_from_config(dict(legacy_cfg, assets=[
    {'code': 'AQUA', 'issuer': _AQUA},
    {'code': 'USDC', 'issuer': _USDC},
    {'code': 'EURC', 'issuer': 'G' + 'A' * 55}]))
check('capped at 2 extra assets', len(got) == 2)

# Malformed legs are dropped, valid ones survive -- a bad leg must not disable the good.
got = portfolio.assets_from_config(dict(legacy_cfg, assets=[
    {'code': 'AQUA', 'issuer': 'GBNZ...'},       # elided issuer
    {'code': 'NOISSUER'},                         # no issuer at all
    'not-a-dict',
    {'code': 'USDC', 'issuer': _USDC}]))
check('malformed legs dropped, valid kept',
      [a['code'] for a in got] == ['USDC'], str(got))

# XLM can never be smuggled in as an "extra" asset.
got = portfolio.assets_from_config(dict(legacy_cfg, assets=[
    {'code': 'XLM', 'issuer': None},
    {'code': 'XLM', 'issuer': _AQUA}]))
check('XLM rejected as extra asset', got == [], str(got))

# Two issuers of the same code in one strategy: second loses.
got = portfolio.assets_from_config(dict(legacy_cfg, assets=[
    {'code': 'AQUA', 'issuer': _AQUA},
    {'code': 'AQUA', 'issuer': _IMPOSTOR}]))
check('duplicate code deduped', len(got) == 1 and got[0]['issuer'] == _AQUA)

# Exact duplicate leg.
got = portfolio.assets_from_config(dict(legacy_cfg, assets=[
    {'code': 'AQUA', 'issuer': _AQUA}, {'code': 'AQUA', 'issuer': _AQUA}]))
check('duplicate spec deduped', len(got) == 1)

for label, bad in [('None', None), ('list', []), ('assets str', {'assets': 'x'}),
                   ('assets None', {'assets': None}), ('assets int', {'assets': 3})]:
    try:
        ok = portfolio.assets_from_config(bad) == []
    except Exception as e:
        ok = False
    check(f'bad config tolerated ({label})', ok)

# ------------------------------------------------------------------------------ done

if _failures:
    print(f'FAILED {len(_failures)} of {_passed + len(_failures)} checks:\n')
    for f in _failures:
        print(f'  - {f}')
    sys.exit(1)

print(f'ok - {_passed} checks passed')

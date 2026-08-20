#!/usr/bin/env python3
"""Standalone self-test for the domain boundary. Proves the refactor was inert.

There is no test runner in this repo (no pytest, no CI), so this is a plain script, the
same shape as tools/selftest_portfolio.py.

RUN IT IN THE CONTAINER, via the wrapper that sets the environment up correctly:

    docker exec <container> /opt/selftest.sh

Exits 0 and prints a pass count, or exits 1 listing every failure.

Do not run it straight out of the host checkout. It will work -- nothing here needs /opt to
import, which is why domain_sdex imports strat_manager lazily -- but without /opt/tools a
dozen paths take their degraded branch (`_tools()` returns None, replay and importability
cannot be consulted, the seeded band falls back to a constant instead of being picked from
real candles), so the run is much weaker than its pass count suggests. It prints which mode
it ran in. /opt/selftest.sh also isolates the tool caches so the run cannot dirty a repo
that check_boundary_integrity halts live trading on, and checks the container's own health
on the way past.

WHAT MAKES THIS WORTH TRUSTING
==============================
The pre-refactor implementation is still in git, and it is import-clean behind its
`__main__` guard. So the strong assertions here are DIFFERENTIAL: load the last monitor.py
that predates the refactor, run the old function and the new domain member over the same
inputs, and require the same answer. That is a far better oracle than expected values
written by hand, because the thing being protected is "no decision changed", not "the
gates are correct" -- the gates' correctness is already argued at length in their
docstrings and was never in question.

Two assertions are not differential and are the most important ones anyway:

  * the contract check over every shipped domain -- sdex, null, and now forecast
    (FUTURE.md item 3's real benchmark). domain_null has no prices, no assets, no order
    book and no money; domain_forecast has all of that too, but is a REAL game (an
    independently-judged Brier score against resolved questions) rather than null's
    admittedly fake "trust a number the strategy wrote down" placeholder. This is the
    evidence that the boundary generalizes, not just that it can be typed to.
  * driving monitor's real smoke-test harness with DOMAIN=null. Reading the loop is not
    enough to show that no sdex assumption leaked into it; running it is.

RUNNING OUTSIDE THE CONTAINER
=============================
Deliberately supported, and it is why domain_sdex imports strat_manager lazily. Outside
/opt there is no /opt/tools, so `_tools()` returns None and the asset/basis paths take
their degraded branch -- but BOTH sides take it, so the differential still means
something, and it is the only way to run this before deploying. It says which mode it ran
in. Run it in the container too before letting a cycle go: only there do the asset gates,
the recorded basis distribution and the real population get exercised.
"""
import copy
import hashlib
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Short enough that the two smoke-test runs below take seconds rather than four minutes.
# Set before importing monitor, which reads it at import.
os.environ.setdefault('SMOKE_TEST_SECONDS', '3')

import domain

# How a pre-refactor monitor.py is recognised: three functions that lived in it and now
# live in domain_sdex. Used to FIND the baseline rather than trusting a hardcoded sha,
# because this file has to work in two different git repos.
#
# The outer mirror repo has master_agent/monitor.py and a history going back years. The
# container's /opt/master_agent is a SEPARATE repo, laid out flat (monitor.py at the root),
# with its own short history written by emperor.sh's self-revision passes. The commit id of
# "the last pre-refactor monitor.py" is therefore different in each, and in the container it
# is whatever the operator committed the refactor on top of. Searching for the content is
# the only thing that works in both without configuration.
#
# Override with DOMAIN_BASELINE_COMMIT to pin a specific commit; it is still checked against
# these markers, so pinning the wrong one is reported rather than silently believed.
_PRE_REFACTOR_MARKERS = ('def _config_is_sane(', 'def fetch_marks_for_cycle(',
                         'def apply_seed_thresholds(')

# REVISION_SYSTEM_PROMPT's hash at the baseline, measured outside the container (so
# friction/caps fall back to 12.0/100.0/4.0/0.50). A cross-check on the differential
# below, not a substitute for it: this constant is environment-dependent and the
# differential is not.
BASELINE_PROMPT_SHA = '26127ec4e9e8b30bd0bd7817f2931389490a57dfd7ca6d8f5ca2e1e75abb2957'

_passed = 0
_failures = []
_skipped = []


def check(label, condition, detail=''):
    global _passed
    if condition:
        _passed += 1
    else:
        _failures.append(f'{label}{": " + detail if detail else ""}')


def same(label, old, new):
    """The differential assertion. Reports both values when they disagree."""
    check(label, old == new, f'baseline {old!r} != domain {new!r}')


def differs(label, old, new, why):
    """The opposite assertion: this behaviour was changed ON PURPOSE, so it must differ.

    Added with the extra-asset removal (2026-08-13). Deleting a differential check when
    you intend to change what it pins is the easy move and the wrong one -- it leaves no
    record, and it stops noticing if the old behaviour ever comes back. This fails in
    both directions: if the two agree, either the removal was undone or the baseline
    never had the behaviour, and both deserve a red run.

    Prefer comparing modulo the intended difference (see the `assets`-key comparisons
    further down) wherever the rest of the value can still be pinned. Use this only where
    the whole value changed shape.
    """
    check(f'{label} [intentionally diverges: {why}]', old != new,
          f'baseline and domain both {old!r}, but this was supposed to change')


def without_assets(text_or_obj):
    """A config dict with the retired `assets` key removed. Accepts JSON text or a dict.

    The extra-asset removal's whole config-side footprint is "this key is gone", so every
    seed_config/tweak_config comparison below can still pin every OTHER key against the
    pre-refactor baseline by normalising just this one away. That keeps the differential
    doing its job over the mechanical mutation paths -- the control arm the revised
    batches are measured against -- instead of dropping those checks wholesale.
    """
    obj = json.loads(text_or_obj) if isinstance(text_or_obj, str) else dict(text_or_obj)
    obj.pop('assets', None)
    return obj


def skip(label, why):
    _skipped.append(f'{label} ({why})')


# ---------------------------------------------------------------- loading the baseline

def _git_show(repo, ref):
    """`git -C repo show ref` stdout, or None."""
    r = subprocess.run(['git', '-C', str(repo), 'show', ref],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _layouts():
    """The (repo, path prefix) pairs monitor.py might live under, nearest first.

    ('', so monitor.py at the repo root) is the container's /opt/master_agent;
    ('master_agent/') is the outer mirror repo. Trying both is what lets one script run in
    either place, which matters because the differential is the whole value of this file
    and it is silently worthless if the baseline cannot be found.
    """
    here = Path(__file__).resolve().parent
    return [(here, ''), (here.parent, 'master_agent/')]


def _find_baseline(max_commits=200):
    """(repo, prefix, commit) for the newest pre-refactor monitor.py, or None.

    Content-addressed rather than configured: walk each candidate repo's history for
    monitor.py and take the first commit whose blob still defines the functions that moved
    into domain_sdex. A pinned DOMAIN_BASELINE_COMMIT is verified the same way instead of
    being trusted.
    """
    forced = os.environ.get('DOMAIN_BASELINE_COMMIT')
    for repo, prefix in _layouts():
        if not (repo / '.git').exists():
            continue
        path = f'{prefix}monitor.py'
        if forced:
            commits = [forced]
        else:
            log = subprocess.run(['git', '-C', str(repo), 'log', '--format=%H',
                                  f'-n{max_commits}', '--', path],
                                 capture_output=True, text=True)
            commits = log.stdout.split() if log.returncode == 0 else []
        for commit in commits:
            blob = _git_show(repo, f'{commit}:{path}')
            if blob and all(m in blob for m in _PRE_REFACTOR_MARKERS):
                return repo, prefix, commit
    return None


_BASELINE = _find_baseline()
BASELINE_COMMIT = _BASELINE[2][:12] if _BASELINE else '(not found)'


def _load_baseline_monitor():
    """The pre-refactor monitor.py, imported as its own module. None if unavailable.

    strat_manager is stubbed, not imported: it mkdirs /opt/strategies as an import side
    effect. This stub is not scoped to baseline monitor alone, though -- it caches into
    sys.modules['strat_manager'] for the rest of the process, so the CURRENT monitor.py
    imported below (section 5) gets it too, on purpose: see the `_strategy_python`
    monkeypatch there ("no strat_manager outside /opt"), which exists because this
    self-test is meant to run outside the container, where the real strat_manager's
    module-level mkdir has no /opt to land in. So every name current or baseline
    monitor.py reaches for on strat_manager has to be shimmed here, not just whatever
    baseline monitor.py happens to want. BIRTH_FILE is a plain filename constant
    (strat_manager.BIRTH_FILE = 'birth.json'), not a fact that drifts like a price or a
    haircut, so duplicating its value here is safe.
    """
    if _BASELINE is None:
        return None
    repo, prefix, commit = _BASELINE
    source = _git_show(repo, f'{commit}:{prefix}monitor.py')
    if source is None:
        return None
    if 'strat_manager' not in sys.modules:
        stub = types.ModuleType('strat_manager')
        stub._strategy_python = lambda: sys.executable
        stub.BIRTH_FILE = 'birth.json'
        sys.modules['strat_manager'] = stub
    path = Path(tempfile.mkdtemp(prefix='domain_baseline_')) / 'baseline_monitor.py'
    path.write_text(source)
    spec = importlib.util.spec_from_file_location('baseline_monitor', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['baseline_monitor'] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_master_agent(baseline=None):
    """master-agent.py (hyphen: not importable normally), from disk or at the baseline.

    Third-party imports are stubbed so this works without the container's venv. Loaded
    under a unique module name each time so baseline and current can coexist.
    """
    for name in ('httpx', 'ollama', 'pydantic', 'requests'):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules['ollama'].Client = lambda *a, **k: types.SimpleNamespace()
    sys.modules['ollama'].ResponseError = type('ResponseError', (Exception,), {})
    os.environ.setdefault('OLLAMA_API_KEY', 'selftest-stub')
    here = Path(__file__).resolve().parent
    if baseline is None:
        path = here / 'master-agent.py'
        label = 'current'
    else:
        repo, prefix, commit = baseline
        source = _git_show(repo, f'{commit}:{prefix}master-agent.py')
        if source is None:
            skip(f'loading master-agent.py at {commit[:12]}',
                 f'not in {repo} at that commit')
            return None
        scratch = Path(tempfile.mkdtemp(prefix='domain_baseline_ma_'))
        path = scratch / 'master_agent_at.py'
        path.write_text(source)
        # It resolves tools.json relative to its own __file__ and reads it at import.
        # Prefer the baseline's copy; fall back to the current one, which only affects
        # TOOL_SCHEMAS and not the prompt this compares.
        schemas = _git_show(repo, f'{commit}:{prefix}tools.json')
        if schemas is not None:
            (scratch / 'tools.json').write_text(schemas)
        else:
            shutil.copy(here / 'tools.json', scratch / 'tools.json')
        label = commit[:12]
    spec = importlib.util.spec_from_file_location(f'ma_{label.replace("-", "_")}', path)
    mod = importlib.util.module_from_spec(spec)
    # Section 4 ("the revision prompt, differentially") validates master-agent.py's
    # SDEX-flavored prompt/facts against the pre-refactor baseline -- that check's
    # validity has nothing to do with which domain this container currently has selected
    # for live running. But domain.get()'s no-argument fallback reads domain.DOMAIN_NAME,
    # a module constant fixed once at domain.py's own import time from the DOMAIN env var
    # -- so on a container whose /opt/env.sh sets DOMAIN=forecast (this one, since
    # FUTURE.md item 3), loading master-agent.py here resolves _DOMAIN to domain_forecast
    # and CUR_MA._FACTS has no 'unrealized_haircut' key at all, which is exactly the
    # KeyError that broke this on 2026-08-06 -- not from selftest.sh itself (which never
    # sources env.sh) but from any invocation that did source it first, e.g. one modeled
    # on once.sh's own ". env.sh" pattern. Force sdex for the load, regardless of the
    # live environment, then restore -- this is a differential against a fixed baseline,
    # not a check of whatever is currently selected.
    import domain as _domain_module
    _prior_domain_name = _domain_module.DOMAIN_NAME
    _domain_module.DOMAIN_NAME = 'sdex'
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        skip(f'loading master-agent.py ({label})', f'{type(e).__name__}: {e}')
        return None
    finally:
        _domain_module.DOMAIN_NAME = _prior_domain_name
    return mod


# ---------------------------------------------------------------------------- fixtures

_PRICE = 0.1666

# Every interesting shape a config.json has actually turned up in, from the failures the
# gates' docstrings describe. Verbatim-realistic rather than minimal: the point is to hit
# each branch of each gate on both sides of the differential.
_CONFIGS = {
    'healthy': {'name': 'X', 'schema_version': 2, 'buy_below': 0.1633,
                'sell_above': 0.1699, 'trade_amount_usd': 10.0, 'assets': []},
    'no schema_version': {'name': 'X', 'buy_below': 0.1633, 'sell_above': 0.1699,
                          'trade_amount_usd': 10.0},
    'no name': {'schema_version': 2, 'buy_below': 0.1633, 'sell_above': 0.1699},
    'wrong name': {'name': 'someone_else', 'schema_version': 2, 'buy_below': 0.1633,
                   'sell_above': 0.1699, 'trade_amount_usd': 10.0},
    'inverted band': {'name': 'X', 'schema_version': 2, 'buy_below': 0.20,
                      'sell_above': 0.16, 'trade_amount_usd': 10.0},
    'zero band': {'name': 'X', 'schema_version': 2, 'buy_below': 0.0,
                  'sell_above': 0.0, 'trade_amount_usd': 10.0},
    'band far below spot': {'name': 'X', 'schema_version': 2, 'buy_below': 0.05,
                            'sell_above': 0.06, 'trade_amount_usd': 10.0},
    'band far above spot': {'name': 'X', 'schema_version': 2, 'buy_below': 0.9,
                            'sell_above': 1.1, 'trade_amount_usd': 10.0},
    # Straddle the anchor multipliers exactly, so the differential pins the 0.5x/1.5x in
    # _thresholds_are_sane rather than only the obviously-bad bands above. At price
    # 0.1666 the floor is 0.0833 and the ceiling is 0.2499; each of these is on the
    # rejecting side of one bound and would be accepted if that bound moved one notch.
    'buy just below the 0.5x floor': {'name': 'X', 'schema_version': 2,
                                      'buy_below': 0.075, 'sell_above': 0.09,
                                      'trade_amount_usd': 10.0},
    'buy just above the 0.5x floor': {'name': 'X', 'schema_version': 2,
                                      'buy_below': 0.0834, 'sell_above': 0.09,
                                      'trade_amount_usd': 10.0},
    'sell just above the 1.5x ceiling': {'name': 'X', 'schema_version': 2,
                                         'buy_below': 0.1633, 'sell_above': 0.26,
                                         'trade_amount_usd': 10.0},
    'sell just below the 1.5x ceiling': {'name': 'X', 'schema_version': 2,
                                         'buy_below': 0.1633, 'sell_above': 0.2498,
                                         'trade_amount_usd': 10.0},
    'missing band': {'name': 'X', 'schema_version': 2, 'trade_amount_usd': 10.0},
    'string band': {'name': 'X', 'schema_version': 2, 'buy_below': 'cheap',
                    'sell_above': 'dear', 'trade_amount_usd': 10.0},
    'zero size': {'name': 'X', 'schema_version': 2, 'buy_below': 0.1633,
                  'sell_above': 0.1699, 'trade_amount_usd': 0.0},
    'basis gate 20bp': {'name': 'X', 'schema_version': 2, 'buy_below': 0.1633,
                        'sell_above': 0.1699, 'trade_amount_usd': 10.0,
                        'basis_min_bp': 20},
    'basis gate non-numeric': {'name': 'X', 'schema_version': 2, 'buy_below': 0.1633,
                               'sell_above': 0.1699, 'trade_amount_usd': 10.0,
                               'basis_min_bp': 'wide'},
    'basis gate null': {'name': 'X', 'schema_version': 2, 'buy_below': 0.1633,
                        'sell_above': 0.1699, 'trade_amount_usd': 10.0,
                        'basis_min_bp': None},
    'invented knobs': {'name': 'X', 'schema_version': 2, 'buy_below': 0.1633,
                       'sell_above': 0.1699, 'trade_amount_usd': 10.0,
                       'rsi_period': 14, 'stop_loss_pct': 0.05,
                       'news_veto_below': -0.4},
    'assets XLM': {'name': 'X', 'schema_version': 2, 'buy_below': 0.1633,
                   'sell_above': 0.1699, 'trade_amount_usd': 10.0,
                   'assets': [{'code': 'XLM', 'issuer': None, 'buy_below': 1,
                               'sell_above': 2, 'trade_amount_usd': 2.5}]},
    'assets malformed': {'name': 'X', 'schema_version': 2, 'buy_below': 0.1633,
                         'sell_above': 0.1699, 'trade_amount_usd': 10.0,
                         'assets': ['AQUA']},
    'assets not a list': {'name': 'X', 'schema_version': 2, 'buy_below': 0.1633,
                          'sell_above': 0.1699, 'trade_amount_usd': 10.0,
                          'assets': 'AQUA'},
}

_STATES = {
    'all cash': {'balance_usd': 1013.4486359716716, 'balance_xlm': 0.0},
    'fully invested': {'balance_usd': 0.0, 'balance_xlm': 5824.719188789025},
    'mixed': {'balance_usd': 500.0, 'balance_xlm': 3000.0},
    'zero': {'balance_usd': 0.0, 'balance_xlm': 0.0},
    'negative usd': {'balance_usd': -1.0, 'balance_xlm': 10.0},
    'v2 positions': {'balance_usd': 100.0, 'balance_xlm': 0.0, 'schema_version': 2,
                     'positions': {'XLM': {'amount': 4000.0, 'code': 'XLM'}}},
    'with history': {'balance_usd': 1000.0, 'balance_xlm': 0.0,
                     'price_history': [0.171397, 0.171412], 'last_buy_price': None},
}


def _scratch(cfg):
    """A throwaway strategy dir holding `cfg` as config.json. Returns the dir."""
    d = Path(tempfile.mkdtemp(prefix='domain_selftest_'))
    json.dump(cfg, open(d / 'config.json', 'w'), indent=2)
    return d


# ============================================================== 1. the contract itself

SDEX = domain.get('sdex')
NULL = domain.get('null')
FORECAST = domain.get('forecast')
MAKER = domain.get('sdex_maker')
# Neither of these is yet folded into the obs-type/encode-decode/differential sections
# below (those pin behavior against a pre-refactor monitor.py that predates both domains
# entirely) -- but a domain this file never even imports is a domain domain.check() has
# never run against, which is exactly the class of bug ("kalshi is missing a required
# contract member") that adding RANK_GRACE_S below could otherwise introduce silently.
KALSHI = domain.get('kalshi')

for mod in (SDEX, NULL, FORECAST, MAKER, KALSHI):
    problems = domain.check(mod)
    check(f'{mod.NAME} satisfies the contract', not problems, '; '.join(problems))

check('DOMAIN_NAME defaults to sdex',
      domain.get().NAME == os.environ.get('DOMAIN', 'sdex'))
check('the registry is cached', domain.get('sdex') is SDEX)

# RANK_GRACE_S used to be the single constant YOUNG_GRACE_S, applied by monitor.py to
# every domain alike -- see domain.py's contract entry for why it became per-domain.
# sdex/null/forecast/sdex_maker all resolve fast enough that the old flat 3h default
# still applies; pin that so nobody "tunes" it differently by accident. kalshi is the one
# domain this was built for, so its value must genuinely differ, not just be present.
check('sdex/null/forecast/maker RANK_GRACE_S is unchanged from the old shared default (3h)',
      SDEX.RANK_GRACE_S == NULL.RANK_GRACE_S == FORECAST.RANK_GRACE_S
      == MAKER.RANK_GRACE_S == 3 * 3600,
      f'{SDEX.RANK_GRACE_S}, {NULL.RANK_GRACE_S}, {FORECAST.RANK_GRACE_S}, '
      f'{MAKER.RANK_GRACE_S}')
check('kalshi RANK_GRACE_S exceeds the generic default to match slow real-world resolution',
      KALSHI.RANK_GRACE_S > 3 * 3600, KALSHI.RANK_GRACE_S)

# ------------------------------------------------------------------ the live kill switch
#
# DOMAIN.live_enabled() is the coarse "may real money move at all right now" gate, as
# opposed to can_execute_live's per-strategy "could THIS code place an order". It exists
# because there was no domain-agnostic way to say "run this population, but not for real":
# a money-free domain hardcoded can_execute_live to False, and sdex could only be stopped
# by a file behind the money boundary that the loop never looked at, so monitor went on
# promoting and flagging strategies whose every order was being refused.
#
# It fails CLOSED, so most of what is worth pinning is that it says NO in the ambiguous
# cases -- including a typo'd on-value, which is the one people get wrong.

for mod in (SDEX, NULL, FORECAST, MAKER, KALSHI):
    verdict = mod.live_enabled()
    check(f'{mod.NAME} live_enabled returns (bool, str)',
          isinstance(verdict, tuple) and len(verdict) == 2
          and isinstance(verdict[0], bool) and isinstance(verdict[1], str), repr(verdict))

for mod in (NULL, FORECAST, KALSHI):
    ok, why = mod.live_enabled()
    check(f'{mod.NAME} can never be switched live', ok is False and bool(why), repr(why))

_saved_live_env = os.environ.get(domain.LIVE_ENV_VAR)
_saved_live_file = domain.LIVE_DISABLED_FILE
_switch_dir = Path(tempfile.mkdtemp(prefix='live_switch_'))
try:
    os.environ.pop(domain.LIVE_ENV_VAR, None)
    domain.LIVE_DISABLED_FILE = _switch_dir / '.live_disabled'
    check('unset env and no sentinel leaves live ON -- every existing deployment sets '
          'neither, so the default must not change under them',
          domain.live_switch() == (True, ''), repr(domain.live_switch()))
    for _value in ('1', 'on', 'yes', 'true', 'ENABLED', 'Live', ' on '):
        os.environ[domain.LIVE_ENV_VAR] = _value
        check(f'{domain.LIVE_ENV_VAR}={_value!r} keeps live on',
              domain.live_switch()[0] is True, repr(domain.live_switch()))
    for _value in ('0', 'off', 'no', 'false', 'paper', 'of', '', 'yse'):
        os.environ[domain.LIVE_ENV_VAR] = _value
        _ok, _why = domain.live_switch()
        check(f'{domain.LIVE_ENV_VAR}={_value!r} switches live off (unrecognised means '
              f'off, so a typo fails safe)',
              _ok is False and domain.LIVE_ENV_VAR in _why, repr(_why))
    os.environ.pop(domain.LIVE_ENV_VAR, None)

    domain.LIVE_DISABLED_FILE.write_text('winding down for the weekend\nignored second line\n')
    _ok, _why = domain.live_switch()
    check('the sentinel file switches live off',
          _ok is False and str(domain.LIVE_DISABLED_FILE) in _why, repr(_why))
    check('the sentinel file explains itself in the reason, first line only',
          'weekend' in _why and 'ignored second line' not in _why, repr(_why))
    # A domain that moves money must consult the generic switch FIRST -- before anything
    # environmental, so an operator's "stop" cannot be outvoted by a healthy money
    # boundary. Checked here rather than by reading the source because the ordering is
    # the whole guarantee.
    for _mod in (SDEX, MAKER):
        _ok, _why = _mod.live_enabled()
        check(f'{_mod.NAME} defers to the generic switch before anything else',
              _ok is False and str(domain.LIVE_DISABLED_FILE) in _why, repr(_why))
finally:
    domain.LIVE_DISABLED_FILE = _saved_live_file
    if _saved_live_env is None:
        os.environ.pop(domain.LIVE_ENV_VAR, None)
    else:
        os.environ[domain.LIVE_ENV_VAR] = _saved_live_env
    shutil.rmtree(_switch_dir, ignore_errors=True)

# Nothing in the loop may need an attribute off `obs`, so every domain's observation type
# is free to differ. Assert they actually do -- if they were the same shape, this whole
# file would be testing one domain twice.
_sdex_obs = SDEX.Observation(price=_PRICE, marks={'XLM': _PRICE})
_null_obs = NULL.Observation(tick=0)
_forecast_obs = FORECAST.Observation(tick=0)
# The maker observes a BOOK, not a scalar -- which is why re-exporting sdex's Observation
# would both fail the check below and be wrong. A taker needs one price to compare a
# threshold against; a maker needs to know what is already resting where, because that is
# its queue position.
_maker_obs = MAKER.Observation(mid=_PRICE, bid=_PRICE * 0.9995, ask=_PRICE * 1.0005,
                               spread_bp=10.0, bid_depth_usd=1000.0,
                               ask_depth_usd=1000.0, cex_mid=_PRICE,
                               bids=[{'p': _PRICE * 0.9995, 'usd': 5.0}],
                               asks=[{'p': _PRICE * 1.0005, 'usd': 5.0}],
                               marks={'XLM': _PRICE})
check('the four domains have pairwise-unrelated observation types',
      len({type(_sdex_obs), type(_null_obs), type(_forecast_obs),
           type(_maker_obs)}) == 4)

# ============================================== 2. observation encode/decode round trip
#
# The quietest failure surface in the refactor: a decode mismatch makes revise_strategy
# exit non-zero, _run_revision return False, and every newcomer fall back to a mechanical
# tweak -- which reads in the log as an ordinary cycle. That is the exact shape of the bug
# that hid the dead revision layer for weeks.

for label, mod, obs in (('sdex', SDEX, _sdex_obs), ('null', NULL, _null_obs),
                        ('forecast', FORECAST, _forecast_obs),
                        ('sdex_maker', MAKER, _maker_obs)):
    encoded = mod.encode_observation(obs)
    # A str is the whole requirement: monitor passes argv as a list, so no shell sees it
    # and spaces are harmless. What is NOT allowed is returning a dict or a tuple, which
    # subprocess would reject with a confusing TypeError inside _run_revision.
    check(f'{label} encodes to a single argv token', isinstance(encoded, str),
          repr(encoded))
    decoded = mod.decode_observation(encoded)
    check(f'{label} observation survives the round trip', decoded is not None)
    check(f'{label} decode("") is None', mod.decode_observation('') is None)
    check(f'{label} decode(None) is None', mod.decode_observation(None) is None)
    check(f'{label} decode(garbage) is None', mod.decode_observation('not an obs') is None)

check('sdex argv 5 is still the bare price string, as before the refactor',
      SDEX.encode_observation(_sdex_obs) == str(_PRICE),
      SDEX.encode_observation(_sdex_obs))
check('sdex decodes a hand-typed price',
      SDEX.decode_observation('0.1666').price == 0.1666)
check('sdex round-trips the price exactly',
      SDEX.decode_observation(SDEX.encode_observation(_sdex_obs)).price == _PRICE)
check('sdex also accepts a structured observation',
      SDEX.decode_observation(json.dumps({'price': 0.2, 'marks': {}})).price == 0.2)

# The two branches of the old price_line, verbatim.
check('observation_line states the price as ground truth',
      SDEX.observation_line(_sdex_obs) ==
      f'Current XLM/USD price (fetched from CoinGecko by monitor.py moments ago, '
      f'this is ground truth -- not a historical or typical price): ${_PRICE}\n')
check('observation_line(None) is the NOT PROVIDED branch',
      SDEX.observation_line(None) ==
      'Current XLM/USD price: NOT PROVIDED for this cycle -- fetch it yourself '
      '(exec curl, or /opt/tools/price_feed.py) before setting any thresholds.\n')

# ==================================== 2b. the extra-asset stack is gone and stays gone
#
# Removed 2026-08-13. These are cheap and they are the check that notices a well-meaning
# emperor pass (or a revision with exec access) putting any of it back: every name below
# was a live entry point into multi-asset trading, and a partial restoration -- an
# injector with no verification gate, say -- is worse than either state.

for _gone in ('_assets_are_sane', '_inject_discovered_assets', '_inject_reflector_assets',
              '_next_candidate_assets', '_next_discovered_assets', '_next_reflector_assets',
              '_record_admission', '_leg_round_trip', 'VERIFIED_ASSETS_FILE',
              'ASSET_APPROVAL_TTL', 'REFLECTOR_INJECT_CHANCE', 'REFLECTOR_INJECT_COUNT'):
    check(f'domain_sdex.{_gone} is gone', not hasattr(SDEX, _gone))

check('sdex marks carry XLM and nothing else',
      list(SDEX.observe_population(SDEX.Observation(price=_PRICE), {}).marks) == ['XLM'])
check('prepare_live has nothing left to prepare',
      SDEX.prepare_live('nonexistent_strategy') == {})

import score as _score_check
for _gone in ('ILLIQUID_HAIRCUT', 'STALE_MARK_MAX_AGE'):
    check(f'score.{_gone} is gone', not hasattr(_score_check, _gone))

# sanitize_config's whole remaining job: delete the key wherever it is inherited from.
for _label, _payload in (('a populated list', [{'code': 'AQUA', 'issuer': 'GBNZ'}]),
                         ('an empty list', []),
                         ('a non-list', 'AQUA')):
    _d = _scratch({'name': 'X', 'schema_version': 2, 'buy_below': 0.1633,
                   'sell_above': 0.1699, 'trade_amount_usd': 10.0, 'assets': _payload})
    try:
        SDEX.sanitize_config(_d / 'config.json', _sdex_obs)
        _after = json.loads((_d / 'config.json').read_text())
        check(f'sanitize_config strips assets [{_label}]', 'assets' not in _after)
        check(f'sanitize_config keeps every other key [{_label}]',
              _after.get('buy_below') == 0.1633 and _after.get('trade_amount_usd') == 10.0,
              repr(_after))
    finally:
        shutil.rmtree(_d, ignore_errors=True)

# ================================================== 3. differential against the baseline

BASE = _load_baseline_monitor()

# A FAILURE, not a skip. Everything below this line is the reason this file exists, and a
# run that cannot find a baseline would otherwise print "ok - N checks passed" having
# verified almost nothing -- which is worse than a red run, because it reads as evidence.
check('a pre-refactor monitor.py was found to compare against', BASE is not None,
      f'searched for {", ".join(_PRE_REFACTOR_MARKERS)} in the monitor.py history of '
      + ' and '.join(f'{r}/{p}' for r, p in _layouts())
      + '; pin one with DOMAIN_BASELINE_COMMIT')

if BASE is None:
    skip('every differential check', 'no pre-refactor monitor.py found')
else:
    IN_CONTAINER = Path('/opt/tools').exists()

    # ---- the four gates, plus the composed verdict, over every fixture
    for label, cfg in _CONFIGS.items():
        # config_is_sane lost its fourth term (_assets_are_sane) with the extra-asset
        # removal, so it now diverges from the baseline on exactly the configs the
        # baseline rejected FOR their assets block and on no others. Branching on the
        # baseline's own asset verdict states that rule precisely, and keeps the check
        # environment-independent: outside the container _assets_are_sane could not
        # consult /opt/tools and passed everything, so there is nothing to diverge from.
        base_assets_ok = BASE._assets_are_sane(copy.deepcopy(cfg), _sdex_obs.marks)
        base_verdict = BASE._config_is_sane(copy.deepcopy(cfg), _PRICE,
                                            _sdex_obs.marks, name='X')
        new_verdict = SDEX.config_is_sane(copy.deepcopy(cfg), 'X', _sdex_obs)
        if base_assets_ok:
            same(f'config_is_sane [{label}]', base_verdict, new_verdict)
        else:
            differs(f'config_is_sane [{label}]', base_verdict, new_verdict,
                    'an assets block is no longer a reason to reject a whole revision; '
                    'sanitize_config deletes the key instead')
        same(f'_required_keys_are_sane [{label}]',
             BASE._required_keys_are_sane(copy.deepcopy(cfg), 'X'),
             SDEX._required_keys_are_sane(copy.deepcopy(cfg), 'X'))
        same(f'_thresholds_are_sane [{label}]',
             BASE._thresholds_are_sane(copy.deepcopy(cfg), _PRICE),
             SDEX._thresholds_are_sane(copy.deepcopy(cfg), _PRICE))
        same(f'_basis_gate_is_sane [{label}]',
             BASE._basis_gate_is_sane(copy.deepcopy(cfg)),
             SDEX._basis_gate_is_sane(copy.deepcopy(cfg)))
        # No price at all: repair_config and prepare_smoke_config both branch on this,
        # and a wrong answer here silently reverts every revised main.py.
        same(f'_thresholds_are_sane, no price [{label}]',
             BASE._thresholds_are_sane(copy.deepcopy(cfg), None),
             SDEX._thresholds_are_sane(copy.deepcopy(cfg), None))

    # ---- normalize_config: same return value AND same file on disk
    for label, cfg in _CONFIGS.items():
        old_dir, new_dir = _scratch(cfg), _scratch(cfg)
        try:
            same(f'normalize_config returns [{label}]',
                 BASE._normalize_config(old_dir / 'config.json', 'X'),
                 SDEX.normalize_config(new_dir / 'config.json', 'X'))
            same(f'normalize_config writes [{label}]',
                 (old_dir / 'config.json').read_text(),
                 (new_dir / 'config.json').read_text())
        finally:
            shutil.rmtree(old_dir, ignore_errors=True)
            shutil.rmtree(new_dir, ignore_errors=True)

    # ---- repair_config: same repair strings AND same file on disk.
    # Seeded, because _seed_band_half_bp picks with random.choice when regime is readable.
    for label, cfg in _CONFIGS.items():
        old_dir, new_dir = _scratch(cfg), _scratch(cfg)
        try:
            random.seed(1234)
            old_repairs = BASE._repair_config(old_dir / 'config.json', _PRICE, 'X')
            random.seed(1234)
            new_repairs = SDEX.repair_config(new_dir / 'config.json', 'X', _sdex_obs)
            same(f'repair_config reports [{label}]', old_repairs, new_repairs)
            same(f'repair_config writes [{label}]',
                 (old_dir / 'config.json').read_text(),
                 (new_dir / 'config.json').read_text())
        finally:
            shutil.rmtree(old_dir, ignore_errors=True)
            shutil.rmtree(new_dir, ignore_errors=True)

    # ---- seed_config / tweak_config: the two mechanical mutation paths.
    # These are what every unrevised newcomer gets, i.e. the control arm the revised
    # batches are compared against, so a divergence here quietly changes the experiment.
    for label, cfg in _CONFIGS.items():
        old_dir, new_dir = _scratch(cfg), _scratch(cfg)
        try:
            random.seed(4321)
            BASE.apply_seed_thresholds(old_dir / 'config.json', 'X', _PRICE)
            random.seed(4321)
            SDEX.seed_config(new_dir / 'config.json', 'X', _sdex_obs)
            # Modulo the retired key: every other key -- the seeded band, the carried
            # trade size, whatever knob a revision invented -- is still pinned exactly.
            same(f'seed_config writes, less the assets key [{label}]',
                 without_assets((old_dir / 'config.json').read_text()),
                 without_assets((new_dir / 'config.json').read_text()))
            check(f'seed_config writes no assets key [{label}]',
                  'assets' not in json.loads((new_dir / 'config.json').read_text()))
        finally:
            shutil.rmtree(old_dir, ignore_errors=True)
            shutil.rmtree(new_dir, ignore_errors=True)

        parent_dir = _scratch(cfg)
        old_dir, new_dir = _scratch({}), _scratch({})
        try:
            random.seed(9999)
            old_ok = BASE.apply_random_tweak(parent_dir / 'config.json',
                                             old_dir / 'config.json', 'X')
            random.seed(9999)
            new_ok = SDEX.tweak_config(parent_dir / 'config.json',
                                       new_dir / 'config.json', 'X')
            same(f'tweak_config returns [{label}]', old_ok, new_ok)
            same(f'tweak_config writes, less the assets key [{label}]',
                 without_assets((old_dir / 'config.json').read_text()),
                 without_assets((new_dir / 'config.json').read_text()))
            check(f'tweak_config writes no assets key [{label}]',
                  'assets' not in json.loads((new_dir / 'config.json').read_text()))
        finally:
            for d in (parent_dir, old_dir, new_dir):
                shutil.rmtree(d, ignore_errors=True)

    # ---- the unreadable-parent path, which is what routes a clone to a seeded config
    missing = Path(tempfile.mkdtemp(prefix='domain_selftest_')) / 'nope.json'
    same('tweak_config on a missing parent',
         BASE.apply_random_tweak(missing, missing.parent / 'a.json', 'X'),
         SDEX.tweak_config(missing, missing.parent / 'b.json', 'X'))
    bad = missing.parent / 'list.json'
    bad.write_text('[1, 2, 3]')
    same('tweak_config on a non-object parent',
         BASE.apply_random_tweak(bad, missing.parent / 'c.json', 'X'),
         SDEX.tweak_config(bad, missing.parent / 'd.json', 'X'))
    shutil.rmtree(missing.parent, ignore_errors=True)

    # ---- scoring, including the unreadable-state path that sorts to -inf
    for label, state in _STATES.items():
        d = _scratch({'name': 'X'})
        try:
            json.dump(state, open(d / 'state.json', 'w'))
            same(f'score [{label}]',
                 BASE.score_from_strategy_path(str(d), _PRICE, _sdex_obs.marks),
                 SDEX.score_path(str(d), _sdex_obs))
            # marks empty -> score_path passes None -> the XLM-only path, which is what
            # a cycle whose asset tooling was unavailable actually takes.
            same(f'score, no marks [{label}]',
                 BASE.score_from_strategy_path(str(d), _PRICE),
                 SDEX.score_path(str(d), SDEX.Observation(price=_PRICE, marks={})))
            # score() must default the XLM mark from obs.price the way the old
            # score_from_strategy_path did, or a cycle with no asset marks scores the
            # base leg at zero.
            import score as _score_mod
            same(f'score() defaults the XLM mark [{label}]',
                 _score_mod.compute_score_multi(copy.deepcopy(state),
                                                {'XLM': _PRICE}),
                 SDEX.score(copy.deepcopy(state),
                            SDEX.Observation(price=_PRICE, marks={})))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    empty = _scratch({'name': 'X'})       # no state.json at all
    try:
        same('score with no state.json',
             BASE.score_from_strategy_path(str(empty), _PRICE),
             SDEX.score_path(str(empty), _sdex_obs))
        (empty / 'state.json').write_text('{not json')
        same('score with unreadable state.json',
             BASE.score_from_strategy_path(str(empty), _PRICE),
             SDEX.score_path(str(empty), _sdex_obs))
    finally:
        shutil.rmtree(empty, ignore_errors=True)

    # ---- config_signature: the dedup key select_parents ranks on.
    # The baseline's tuple is (buy, sell, size, asset_sig, basis_min_bp); the asset
    # element at index 3 went with the extra-asset stack, so the new tuple is the old one
    # with that position removed. Compared that way rather than dropped, because the
    # remaining four elements decide which parents select_parents treats as duplicates,
    # and a silent change there costs a revision slot per cycle.
    for label, cfg in _CONFIGS.items():
        d = _scratch(cfg)
        try:
            old_sig = BASE._config_signature({'path': str(d)})
            new_sig = SDEX.config_signature({'path': str(d)})
            same(f'config_signature, less the asset element [{label}]',
                 None if old_sig is None else old_sig[:3] + old_sig[4:], new_sig)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    same('config_signature on a missing dir',
         BASE._config_signature({'path': '/nonexistent'}),
         SDEX.config_signature({'path': '/nonexistent'}))

    # ---- caps and the replay/importability gates.
    # caps() now records only the two XLM caps; the five non-base ones described legs
    # that no longer exist. stellar_trader still defines and enforces all seven -- it is
    # the money boundary and was deliberately not touched -- so the baseline still reads
    # them and the two XLM values must still agree exactly.
    _old_caps, _new_caps = BASE._stellar_caps(), SDEX.caps()
    if _old_caps is None or _new_caps is None:
        same('caps (both unavailable)', _old_caps, _new_caps)
    else:
        same('caps, the XLM pair that survived',
             {k: _old_caps[k] for k in ('max_trade_usd', 'max_daily_usd')}, _new_caps)
        check('caps no longer records the non-base caps',
              not any(k.endswith('_nonbase') or 'per_asset' in k or 'stuck' in k
                      for k in _new_caps),
              repr(sorted(_new_caps)))
    same('importability of a good main.py',
         BASE._importability_check('def decide(price, history, state, config):\n    return None\n'),
         SDEX.importability('def decide(price, history, state, config):\n    return None\n'))
    same('importability of a blind main.py',
         BASE._importability_check('import sys\nsys.path.append("/opt/tools")\n'
                                   'def decide(p, h, s, c):\n    return None\n'),
         SDEX.importability('import sys\nsys.path.append("/opt/tools")\n'
                            'def decide(p, h, s, c):\n    return None\n'))
    check('REPLAY_DAYS matches the old BACKTEST_GATE_DAYS',
          SDEX.REPLAY_DAYS == BASE.BACKTEST_GATE_DAYS,
          f'{SDEX.REPLAY_DAYS} != {BASE.BACKTEST_GATE_DAYS}')
    check('REPLAY_WINDOW reproduces the old gate message',
          SDEX.REPLAY_WINDOW == f'{BASE.BACKTEST_GATE_DAYS}d of real candles',
          SDEX.REPLAY_WINDOW)

    # ---- the smoke-test hooks, against the code they were cut out of
    for label, cfg in _CONFIGS.items():
        old_cfg, new_cfg = copy.deepcopy(cfg), copy.deepcopy(cfg)
        # This is the inline block main_py_is_sane used to run before the smoke run.
        if not BASE._thresholds_are_sane(old_cfg, _PRICE):
            old_cfg['buy_below'] = round(_PRICE * 0.98, 6)
            old_cfg['sell_above'] = round(_PRICE * 1.02, 6)
        same(f'prepare_smoke_config [{label}]',
             old_cfg, SDEX.prepare_smoke_config(new_cfg, _sdex_obs))

    check('MAIN_PY_IMPORTABILITY moved with its default',
          SDEX.MAIN_PY_IMPORTABILITY == BASE.MAIN_PY_IMPORTABILITY)

    # check_smoke_state's success value must be only the clause the harness appends, so
    # that "ran 120s, net worth 1000.00" comes back byte-identical.
    ok, detail = SDEX.check_smoke_state(_STATES['all cash'], _CONFIGS['healthy'], _sdex_obs)
    check('check_smoke_state returns a clause, not a sentence',
          ok and detail.startswith('net worth ') and 'ran ' not in detail, repr(detail))
    ok, detail = SDEX.check_smoke_state(_STATES['negative usd'], _CONFIGS['healthy'],
                                        _sdex_obs)
    check('check_smoke_state rejects a negative balance', not ok, repr(detail))
    ok, detail = SDEX.check_smoke_state(_STATES['zero'], _CONFIGS['healthy'], _sdex_obs)
    check('check_smoke_state rejects a zero net worth', not ok, repr(detail))

    # ---- check_replayable must never turn an internal failure into a rejection
    check('check_replayable is None when the gate is off',
          (lambda: (setattr(SDEX, 'MAIN_PY_IMPORTABILITY', 'off'),
                    SDEX.check_replayable('nonsense', None, 'X'),
                    setattr(SDEX, 'MAIN_PY_IMPORTABILITY', BASE.MAIN_PY_IMPORTABILITY))[1]
           )() is None)
    check('check_replayable passes a sighted main.py',
          SDEX.check_replayable('def decide(p, h, s, c):\n    return None\n',
                                None, 'X') is None)

    # ---- the population on disk, if there is one. Dozens to hundreds of real cases.
    state_file = domain.STATE_FILE
    if state_file.exists():
        try:
            population = json.loads(state_file.read_text())
        except Exception:
            population = {}
        for name, entry in list(population.items()):
            cfg_path = Path(entry.get('path', '')) / 'config.json'
            if not cfg_path.exists():
                continue
            try:
                cfg = json.load(open(cfg_path))
            except Exception:
                continue
            if not isinstance(cfg, dict):
                continue
            # Same two allowances as the fixture loop above, for the same reasons: the
            # baseline's asset verdict decides whether config_is_sane may diverge, and
            # config_signature is compared with the baseline's asset element removed.
            # This loop runs over the REAL population, so it is the check that would
            # actually have caught a lineage still carrying a declared leg -- roughly a
            # dozen did at removal time.
            if BASE._assets_are_sane(copy.deepcopy(cfg), _sdex_obs.marks):
                same(f'live config_is_sane [{name}]',
                     BASE._config_is_sane(copy.deepcopy(cfg), _PRICE, _sdex_obs.marks,
                                          name=name),
                     SDEX.config_is_sane(copy.deepcopy(cfg), name, _sdex_obs))
            else:
                differs(f'live config_is_sane [{name}]',
                        BASE._config_is_sane(copy.deepcopy(cfg), _PRICE, _sdex_obs.marks,
                                             name=name),
                        SDEX.config_is_sane(copy.deepcopy(cfg), name, _sdex_obs),
                        'an assets block no longer rejects a config')
            _old_sig = BASE._config_signature(entry)
            same(f'live config_signature, less the asset element [{name}]',
                 None if _old_sig is None else _old_sig[:3] + _old_sig[4:],
                 SDEX.config_signature(entry))
            same(f'live score [{name}]',
                 BASE.score_from_strategy_path(entry['path'], _PRICE, _sdex_obs.marks),
                 SDEX.score_path(entry['path'], _sdex_obs))
            same(f'live activity [{name}]',
                 BASE.trade_stats(name), SDEX.activity(name))
            same(f'live turnover [{name}]',
                 BASE.turnover_stats(name), SDEX.turnover_stats(name))
            same(f'live activity_log_path [{name}]',
                 BASE.trade_log_path(name), SDEX.activity_log_path(name))
            same(f'live can_execute_live [{name}]',
                 BASE.main_py_calls_execute_trade(name), SDEX.can_execute_live(name))
    else:
        skip('the real-population differential', f'{state_file} does not exist')

# =============================================== 4. the revision prompt, differentially

CUR_MA = _load_master_agent()
BASE_MA = _load_master_agent(_BASELINE) if _BASELINE else None
if CUR_MA is None:
    skip('prompt checks', 'could not load master-agent.py from disk')
elif BASE_MA is None:
    skip('prompt differential', f'could not load master-agent.py at {BASELINE_COMMIT}')
else:
    # There used to be a same('REVISION_SYSTEM_PROMPT is byte-identical to the
    # baseline', ...) pair here, proving the 2026 domain-plugin refactor (hardcoded sdex
    # text -> _build_sdex_revision_system_prompt()) did not alter the prompt itself. That
    # was a one-time proof about THAT refactor, not a promise the wording would never
    # change again -- and it was retired 2026-08-11 when the prompt was deliberately
    # rewritten for a token-diet pass (see the "freeze was lifted" comment above
    # _build_sdex_revision_system_prompt in master-agent.py). Keeping it would fail this
    # self-test forever for a change that was intentional; the checks below it, which
    # verify every number the prompt states still matches the module that enforces it,
    # are the ones that still matter and do not depend on exact wording.
    if not Path('/opt/tools').exists():
        check('the baseline prompt hash matches the recorded constant',
              hashlib.sha256(BASE_MA.REVISION_SYSTEM_PROMPT.encode()).hexdigest()
              == BASELINE_PROMPT_SHA)
    # Every fact the prompt states must still come from the module that enforces it.
    facts = CUR_MA._FACTS
    same('prompt haircut', BASE_MA.score.UNREALIZED_HAIRCUT, facts['unrealized_haircut'])
    same('prompt XLM round trip', BASE_MA._XLM_RT_BP, facts['xlm_round_trip_bp'])
    same('prompt max trade', BASE_MA._MAX_TRADE_USD, facts['max_trade_usd'])
    # The baseline also stated a non-base round trip and a non-base per-trade cap. Both
    # described the extra-asset legs and went with them; the prompt no longer mentions
    # either, so prompt_facts must not still be computing them.
    check('prompt_facts no longer states the non-base numbers',
          'nonbase_round_trip_bp' not in facts and 'max_trade_usd_nonbase' not in facts,
          repr(sorted(facts)))
    check('ROLE_* are no longer hand-duplicated',
          CUR_MA.ROLE_REFINE is domain.ROLE_REFINE
          and CUR_MA.ROLE_EXPLORE is domain.ROLE_EXPLORE)
    check('revise-strategy still takes six positional argv',
          CUR_MA.revise_strategy.__code__.co_argcount == 6)

# ============================== 5. the loop, driven against a domain with no money in it
#
# The assertion nothing else can make: monitor.py's real smoke-test harness, over a domain
# that has no price, no order book and no caps. If an sdex assumption is left in the loop,
# this is what finds it.

import monitor

_real_domain = monitor.DOMAIN
monitor._strategy_python = lambda: sys.executable     # no strat_manager outside /opt
monitor.SMOKE_TEST_SECONDS = int(os.environ['SMOKE_TEST_SECONDS'])

_LIVE_MAIN = '''import json
import time

def decide(question, state, config):
    return config.get('confidence', 0.6)

def main():
    state = {'points': 0.0}
    json.dump(state, open('state.json', 'w'))
    while True:
        time.sleep(1)
        state['points'] += 1
        json.dump(state, open('state.json', 'w'))

if __name__ == '__main__':
    main()
'''
_DEAD_MAIN = 'def decide(q, s, c):\n    return 0.5\n'          # no loop; exits at once
_BROKEN_MAIN = 'def decide(q, s, c)\n    return 0.5\n'          # syntax error

for label, source, expect_ok in (('a running main.py', _LIVE_MAIN, True),
                                 ('a main.py that exits', _DEAD_MAIN, False),
                                 ('a main.py that will not parse', _BROKEN_MAIN, False)):
    monitor.DOMAIN = NULL
    d = _scratch({'name': 'nulltest', 'confidence': 0.6, 'questions_per_tick': 1})
    try:
        (d / 'main.py').write_text(source)
        ok, reason = monitor.main_py_is_sane(d, 'nulltest', _null_obs)
        check(f'null domain: the smoke harness accepts {label}' if expect_ok
              else f'null domain: the smoke harness rejects {label}',
              ok is expect_ok, reason)
        if expect_ok:
            check('null domain: the harness formats the domain clause',
                  reason.startswith(f'ran {monitor.SMOKE_TEST_SECONDS}s, ')
                  and 'points' in reason, reason)
    finally:
        monitor.DOMAIN = _real_domain
        shutil.rmtree(d, ignore_errors=True)

# The rest of the genome path, against the same money-free domain.
monitor.DOMAIN = NULL
try:
    d = _scratch({'confidence': 3.0})
    try:
        cfg_path = d / 'config.json'
        check('null domain: normalize_config fills its own keys',
              NULL.normalize_config(cfg_path, 'nulltest') is True)
        check('null domain: repair_config clamps its own knob',
              len(NULL.repair_config(cfg_path, 'nulltest', _null_obs)) == 1)
        cfg = json.load(open(cfg_path))
        check('null domain: config_is_sane accepts the repaired config',
              NULL.config_is_sane(cfg, 'nulltest', _null_obs) is True, json.dumps(cfg))
        check('null domain: config_is_sane rejects a name mismatch',
              NULL.config_is_sane(cfg, 'someone_else', _null_obs) is False)
        NULL.seed_config(cfg_path, 'nulltest', _null_obs)
        check('null domain: seed_config writes a sane config',
              NULL.config_is_sane(json.load(open(cfg_path)), 'nulltest', _null_obs))
        child = d / 'child.json'
        check('null domain: tweak_config mutates a parent',
              NULL.tweak_config(cfg_path, child, 'child') is True)
        check('null domain: the mutation is sane',
              NULL.config_is_sane(json.load(open(child)), 'child', _null_obs))
        check('null domain: replay returns the contract shape',
              set(NULL.replay(d)) >= {'trades', 'beats_null'})
        check('null domain: replay is None on an unreadable dir',
              NULL.replay('/nonexistent') is None)
        check('null domain: caps() is None and nothing minds', NULL.caps() is None)
        check('null domain: can_execute_live fails closed',
              NULL.can_execute_live('nulltest')[0] is False)
        # select_parents and the reporters must not touch an sdex concept either.
        perf = [('a', 1001.0), ('b', 1000.0)]
        state = {'a': {'path': str(d)}, 'b': {'path': str(d)}}
        monitor.select_parents(perf, state, 2)
        monitor.print_idle_report(perf, state, {'a': 1, 'b': 0}, _null_obs)
        NULL.report_activity(perf, 8)
        check('null domain: the loop reporters run', True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
finally:
    monitor.DOMAIN = _real_domain

# The loop must actually HONOUR the switch -- a contract member nothing calls is a switch
# that switches nothing, which is the failure mode this whole file exists to catch. Driven
# against NULL (live_enabled is constant False) with every side effect monkeypatched, so
# it cannot touch a real strategy, a real flag or the integrity baseline.
monitor.DOMAIN = NULL
_gate_saved = (monitor.load_live_strategy, monitor.set_live_flag,
               monitor.check_boundary_integrity, monitor.STRATEGIES_DIR)
_gate_calls = []
_gate_dir = Path(tempfile.mkdtemp(prefix='live_gate_'))
try:
    (_gate_dir / 'held').mkdir()
    (_gate_dir / 'held' / 'main.py').write_text('# placeholder\n')
    (_gate_dir / 'held' / 'live.flag').write_text('')
    monitor.STRATEGIES_DIR = _gate_dir
    monitor.load_live_strategy = lambda: {'name': 'held'}
    monitor.set_live_flag = lambda name, live: _gate_calls.append(('flag', name, live))
    monitor.check_boundary_integrity = lambda: (_gate_calls.append(('integrity',)),
                                                (True, []))[1]
    monitor.promote_live_strategy('held', 1234.0)
    check('a disabled domain has its live flag cleared by the loop',
          ('flag', 'held', False) in _gate_calls, repr(_gate_calls))
    check('a disabled domain promotes nothing and adopts no integrity baseline',
          not any(c[0] == 'integrity' for c in _gate_calls), repr(_gate_calls))
    _ok, _lines = monitor.promote_strategy_manual('held', force=True)
    check('--promote --force cannot override the switch', _ok is False, repr(_lines))
    check('the refusal names the switch to clear',
          any(str(domain.LIVE_DISABLED_FILE) in line for line in _lines), repr(_lines))
finally:
    (monitor.load_live_strategy, monitor.set_live_flag, monitor.check_boundary_integrity,
     monitor.STRATEGIES_DIR) = _gate_saved
    monitor.DOMAIN = _real_domain
    shutil.rmtree(_gate_dir, ignore_errors=True)

# --------------------------------------------------------------------------------- done

mode = 'in the container' if Path('/opt/tools').exists() else 'outside the container'
if _BASELINE:
    _repo, _prefix, _commit = _BASELINE
    print(f'ran {mode}; baseline {_commit[:12]} ({_prefix or ""}monitor.py in {_repo})')
else:
    print(f'ran {mode}; NO BASELINE FOUND -- every differential check was skipped')
for s in _skipped:
    print(f'  SKIP {s}')

if _failures:
    print(f'\nFAILED {len(_failures)} of {_passed + len(_failures)} checks:\n')
    for f in _failures:
        print(f'  - {f}')
    sys.exit(1)

print(f'ok - {_passed} checks passed')

#!/usr/bin/env python3
"""The domain boundary: what the evolutionary loop needs from a money-making game.

READ THIS FIRST IF YOU ARE REVISING monitor.py. monitor.py is the loop -- it scores,
ranks, culls, clones, revises, gates, promotes and sleeps. It knows nothing about XLM,
order books, spreads, assets or prices, and it must stay that way. Everything that IS
about a specific way of making money lives in a domain module (`domain_sdex.py` for the
Stellar DEX, `domain_null.py` for the no-money reference used by selftest_domain.py).
If you are about to add a price, an asset, a threshold or a trade to monitor.py: it goes
in the domain instead, behind one of the members below.

Why this exists: FUTURE.md's stated goal is a *generic* "make money" system, and the
domain was welded into monitor.py at sixteen call sites that threaded a scalar XLM price
and a dict of asset marks through the whole loop. A domain with no prices (a forecasting
benchmark) could not be scored, gated or seeded by that loop at all -- every config would
fail the `buy_below < sell_above` check, which is a fact about the Stellar DEX and not
about evolution.

A domain is a MODULE, not a class. `domain.get()` returns the module and the loop calls
`DOMAIN.score(...)`, `DOMAIN.caps()` and so on. There is no state to carry, the rest of
this codebase is module-level, and it keeps the call sites reading like FUTURE.md's
original sketch.


WHAT A DOMAIN OWES THE LOOP
===========================

FUTURE.md's "What a domain needs to plug into this loop" lists five criteria a candidate
domain must satisfy. Five of the six groups below are those criteria made callable; the
sixth is one the list omits.

1. FITNESS RESOLVABLE INSIDE A CYCLE. The loop culls hourly on rank, so a domain whose
   feedback takes a week cannot be scored by it at all.
     observe()                     -> obs | None   one cheap reading of the world
     observe_population(obs, state) -> obs         enrich it for this population
     OBSERVE_FAILURE_NOTE          str            what to print when observe() gives up
     encode_observation(obs)       -> str         obs across a subprocess boundary
     decode_observation(str)       -> obs | None  and back
     score(state_dict, obs)        -> (float, list_of_unpriced)
     score_path(dir, obs)          -> float | None
     activity(name)                -> (count, first_ts, last_ts)
     activity_log_path(name)       -> Path
     config_signature(state_entry) -> hashable | None
     RANK_GRACE_S                  int            seconds before rank-cull trusts a score

   `config_signature` is here because the loop dedupes the parents it clones, and "these
   two strategies are the same strategy" is a question only the domain can answer -- for
   sdex it is the threshold band, the size and the basis gate. Ranking on a
   signature the loop invented would silently spend both revision slots on one point.

   `obs` is whatever the domain wants -- the loop only ever tests `obs is None` and hands
   it back. Do not make the loop read an attribute of it.

   `RANK_GRACE_S` used to be a single constant monitor.py applied to every domain alike
   (three cycles' worth at monitor.py's own default CYCLE_SLEEP). That is a fact about how
   fast a domain's feedback resolves, not about the loop, and domains differ by orders of
   magnitude on it: domain_forecast's questions resolve in ~30s, domain_kalshi's markets
   take hours to days. A strategy scored on a domain whose feedback hasn't arrived yet sits
   at STARTING_SCORE with no real signal, so a grace period sized for the fast domain culls
   the slow one on a trade-count tiebreak, not evidence -- see domain_kalshi.py's
   RANK_GRACE_S comment for the run this was first observed on.

2. A CHEAP OFFLINE REPLAY. Without one, the gates have nothing to test a revision on and
   LLM-written code reaches money through a check that only proves it parses.
     REPLAY_DAYS, REPLAY_WINDOW    int, str       how much history, and how to say so
     replay(dir)                   -> dict | None  {trades, beats_null, null_pct, raw}
     importability(src_or_path)    -> (ok, reason) | None

   None means "could not be measured" and every caller in the loop FAILS OPEN on it.
   That is deliberate: these guard a fitness signal, not money, and failing closed on a
   tooling outage would revert every revision in the population at once -- a much larger
   and much quieter failure than the blindness it prevents.

3. A NULL STRATEGY TO MEASURE AGAINST. Absolute return is beta, not skill. Carried as
   `beats_null` / `null_pct` inside the replay() result: buy-and-hold for a price domain,
   the market's implied probability for a prediction market, a Brier score against the
   base rate for a forecasting benchmark.

4. CAPS ENFORCED OUTSIDE THE AGENT'S REACH. Never caller-supplied, never overridable by a
   config or a revision -- the revision LLM runs `exec` as root and is told it may edit
   its own tooling, so a cap it can reach is not a cap.
     live_enabled()                -> (ok, reason)     FAILS CLOSED
     caps()                        -> dict | None
     can_execute_live(name)        -> (ok, reason)     FAILS CLOSED
     promotion_sizing(name)        -> dict | None
     prepare_live(name)            -> dict
     retire_live(old_name)         -> (ok, [lines])
     SMOKE_ENV                     dict
     live_track_record(name)       -> (ok, reason)     OPTIONAL, FAILS CLOSED

   SMOKE_ENV is a hard requirement, not a convenience: the loop smoke-tests candidate
   code by RUNNING it, and any domain that can move real money must supply an environment
   in which execution is structurally impossible. `{'PAPER_ONLY': '1'}` for sdex. A domain
   with no money returns `{}`.

   `live_track_record` is the one optional member here, and the only optional member
   anywhere in this contract that guards money. It answers "has this strategy earned real
   money", which `monitor.qualifies_for_live` otherwise decides on trade count, age and
   score alone -- three things that are true of every domain and therefore cannot see a
   score that rose for a reason that is not skill. That reason IS domain knowledge: the
   maker's score marks inventory to market, so a trending market ranks whoever is holding
   the most, and the strategy the loop would promote in a rally is a directional position
   rather than the best market maker. A domain that can tell its own edge from its own
   drift implements this; one that cannot omits it and keeps the old bar exactly. Not in
   CONTRACT for that reason -- `check()` must not fail a domain for declining to answer a
   question it has no way to answer.

   Note `can_execute_live`, `live_enabled` and `live_track_record` are the members that
   fail CLOSED. Every other verdict here guards a fitness signal; these three decide
   whether real money moves, and believing money is deployed behind a position that was
   never opened is worse than not promoting at all.

   The first two answer different questions and neither substitutes for the other:
   `can_execute_live(name)` asks whether THAT STRATEGY is structurally able to place a
   real order; `live_enabled()` asks whether THIS DOMAIN may have a live strategy at all
   right now. Before it existed there was no domain-agnostic way to say "run this
   population, but not for real money" -- a money-free domain hardcoded can_execute_live
   to False, and sdex could only be stopped by tools/stellar_trader.py's own
   /opt/trades/.live_halt, which is behind the money boundary and invisible to the loop:
   monitor went on promoting, writing live.flag and recording promotion sizing for a
   strategy whose every order was refused, which is precisely the "monitor believes money
   is deployed" state the paragraph above is about. Now the loop asks first, clears
   live.flag when the answer is no, and restores it when the answer changes back.

   `live_track_record(name)` is a third and separate question again -- not "can it" and
   not "may we", but "should it": both of the others can be yes about a strategy that has
   never shown an edge. It is a bar, so an operator's `--promote --force` skips it, which
   `can_execute_live` and `live_enabled` never allow.

   `live_switch()` below is the domain-agnostic half every domain that moves money should
   defer to first (the operator's file/env switch); a domain adds its own reasons on top
   -- for sdex, its own .live_halt. A domain with no money returns a constant False, the
   same honest answer its can_execute_live gives.

   It gates PROMOTION and the live flag, not execution: turning it off leaves any open
   real position exactly where it is, for the same reason the boundary-integrity halt
   does -- flattening routes through the money path an operator has just asked to stop
   using. Winding a position down stays a separate, deliberate act.

5. ENOUGH INDEPENDENT BETS PER HOUR THAT RANKING ISN'T NOISE. Deliberately NOT a member.
   This is a property you judge when *choosing* a domain, not something the loop can ask
   at runtime -- and the current system fails it (an hourly cull over a few hours of one
   asset's price path is mostly sampling noise, so the loop clones the winners of coin
   flips). What a domain can supply is the instrument that makes the failure visible:
     report_activity(performances, limit)   turnover vs edge -- is this an edge or a habit?
     stuck_report(performances, state, obs) -> str | None
     report_regime(obs), report_experiments(), report_live(name)
     ensure_background_jobs()               supervise whatever writes the data they read
     background_jobs_alive()  -> bool       for monitor.py --ensure-recorder

6. THE LOOP MUST BE ABLE TO SYNTHESIZE A VALID GENOME AND REJECT AN INVALID ONE. FUTURE.md
   omits this and it is the largest block of domain code in the system. Every cycle the
   loop creates strategies whose config it must be able to build from nothing (a template
   spawn), mutate (an unrevised clone), repair (a revision that got one knob wrong) and
   judge -- none of which it can do without knowing what a config MEANS.
     TEMPLATE_REPO                 str            the seed genome
     STARTING_SCORE                float          what an untouched strategy scores
     normalize_config(path, name)  -> bool        fill keys only the loop can know
     sanitize_config(path, obs)    -> list        re-verify and delete what fails
     repair_config(path, name, obs) -> [lines]    fix what the domain can derive
     config_is_sane(cfg, name, obs) -> bool       the all-or-nothing verdict
     inject_experiments(path, obs) -> bool        mechanically introduce novelty
     seed_config(path, name, obs)  -> None        build one from scratch
     tweak_config(parent, new, name) -> bool      mutate a parent's

   plus the three hooks the smoke-test harness calls, which are part of the same job:
     check_replayable(src, baseline_src, name) -> (ok, reason) | None
     prepare_smoke_config(cfg, obs) -> cfg
     check_smoke_state(raw_state, cfg, obs) -> (ok, detail)
     cleanup_scratch(scratch_name)  -> None

And two groups that are not criteria at all but have to live somewhere:

7. FACTS THE REVISION PROMPT MUST NOT RESTATE.
     prompt_facts()                -> dict
     observation_line(obs)         -> str

   The revision prompt told the model the score haircut was 0.999 for weeks while score.py
   enforced 0.899, so the model was optimizing an objective it was not ranked on. Any
   number the prompt states about the domain is read live through prompt_facts() and never
   written as a literal in prose. Adding a number to a prompt means adding it here.

8. HOW FAST TO RUN IT. OPTIONAL.
     PACING                        dict[str, str]  env vars create.sh writes into env.sh

   Not a member the loop ever calls -- it is read once, on the host, by create.sh, which
   appends `export K=V` for each pair to the v/env.sh it writes. emperor.sh sources that
   file before it reads EMPEROR_RUN_HOURS or launches monitor.py, so both processes come
   up at the domain's cadence with no per-container hand-editing.

   It exists because cadence is domain knowledge and had nowhere to live. CYCLE_SLEEP and
   EMPEROR_RUN_HOURS were baked into the image (supervisor's `environment=` line and
   emperor.sh's own default), one setting for every domain -- and a cadence sized for a
   real exchange is meaningless for a game that resolves in seconds. domain_null exists to
   test the loop, and testing the loop means running cycles; at 8h per cycle it could not.

   Kept as a dict of environment variables, rather than as named constants the loop reads,
   for two reasons. The audience is three different processes (emperor.sh, monitor.py and
   whatever they spawn), only one of which is Python; and every one of these settings
   already HAS an env-var override that an operator can use per-run. This just gives the
   domain a way to set the default.

   Values must be plain single-line strings with no shell metacharacters -- they are
   written into a file that gets sourced as root. check() enforces that; create.sh
   refuses to bootstrap a domain that fails it rather than writing the file anyway.

   Omitting it entirely is fine and means "the image's settings are right for me": every
   domain but null does exactly that.


WHAT IS DELIBERATELY NOT HERE
=============================

* `domain.execute(...)`, which FUTURE.md's sketch proposes. The loop never executes a
  trade: execution happens inside the strategy's own main.py, which is the only thing that
  reads live.flag. A monitor-side execute() would be a seam with no caller -- exactly the
  "abstraction built against one example" trap FUTURE.md warns about. The money boundary
  the loop actually touches is group 4.
* Cross-domain score normalization, per-domain KEEP_TOP_N, and a bandit allocating clone
  budget across domains. FUTURE.md defers these until a second domain exists, and so does
  this. One active domain per process, chosen by the DOMAIN env var.
* A per-strategy domain tag. When two domains must run at once the tag belongs in
  /opt/strategy_state.json, which strat_manager.py owns -- NOT in config.json, which the
  revision LLM rewrites and would eventually use to reassign its own domain.
* The LLM tool surface (sr_agent_tools.py / tools.json), about half of which is
  sdex-specific. The later seam is `domain.llm_tools() -> (callables, schemas)` merged
  with a generic core; it is orthogonal to this boundary.
"""
import ast
import importlib
import inspect
import os
import re
from pathlib import Path

# Which domain this process is playing. One per process, deliberately: see "what is
# deliberately not here" above.
DOMAIN_NAME = os.environ.get('DOMAIN', 'sdex')

# The filesystem contract between the loop and a domain. These live here rather than in
# monitor.py because both sides need them -- monitor owns the registry and the strategy
# directories, a domain owns the logs written into TRADES_DIR -- and a constant with two
# homes is a constant that will drift. monitor.py keeps aliases at the old names.
STATE_FILE = Path('/opt/strategy_state.json')
STRATEGIES_DIR = Path('/opt/strategies')
TRADES_DIR = Path('/opt/trades')
LIVE_STRATEGY_FILE = Path('/opt/live_strategy.json')

# The two jobs a newcomer can be handed. `refine` is a clone of a leader: improve on a
# parent with a real record. `explore` is a fresh template spawn with no meaningful
# parent, asked for something structurally different from the incumbents. The role picks
# which user prompt master-agent.py builds, and is passed to it as the 6th argv of
# `revise-strategy`.
#
# These used to be hand-duplicated in monitor.py and master-agent.py, because
# master-agent.py's filename has a hyphen and cannot be imported. This module can be, by
# both, so the duplication is gone -- keeping two copies of a constant in sync by comment
# is how score.py's haircut ended up two orders of magnitude away from the prompt that
# quoted it.
ROLE_REFINE = 'refine'
ROLE_EXPLORE = 'explore'


# The domain-agnostic half of DOMAIN.live_enabled(): the operator's off switch for real
# execution. Two mechanisms, because they have different reach:
#
#   * LIVE_DISABLED_FILE -- takes effect on the next cycle of an already-running monitor,
#     which is what you want at 3am. Deliberately in /opt and NOT in any repo the revision
#     LLM is invited to edit (/opt/master_agent, /opt/tools) or in a strategy directory:
#     a switch a revision can delete is not a switch. Its contents, if any, are printed
#     as the reason, so `echo 'why' > /opt/.live_disabled` self-documents the halt.
#   * LIVE_ENV_VAR -- set on the process, so `LIVE_TRADING=off ./emperor.sh` brings a
#     whole run up paper-only with nothing to clean up afterwards. Read at call time, but
#     a process's environment cannot be changed from outside, so this one needs a restart.
#
# UNSET MEANS ENABLED (every existing deployment sets neither), but any *set* value that
# is not recognised as an on-value disables: for a safety switch, `LIVE_TRADING=of` must
# fail the safe way, and the cost of that is a typo'd on-value reading as off, which is
# loud (the reason is printed every cycle) rather than silent.
LIVE_DISABLED_FILE = Path('/opt/.live_disabled')
LIVE_ENV_VAR = 'LIVE_TRADING'
LIVE_ON_VALUES = ('1', 'on', 'yes', 'true', 'enabled', 'live')


def live_switch():
    """The operator's real-execution switch, as (enabled, reason). Never raises.

    Not itself a contract member -- a domain's `live_enabled()` is, and one that moves
    money should consult this first and add its own reasons on top (see domain_sdex).
    Kept here rather than duplicated per domain because "the operator turned it off" is
    not domain knowledge, and a kill switch with two implementations is one that will
    disagree with itself about whether it is on.

    Fails CLOSED: if the sentinel cannot even be stat'd, the honest answer is that we do
    not know whether an operator asked for real money to stop, and the safe reading of
    "don't know" is "stop".
    """
    try:
        exists = LIVE_DISABLED_FILE.exists()
    except Exception as e:
        return False, f'could not check {LIVE_DISABLED_FILE} ({e})'
    if exists:
        try:
            note = LIVE_DISABLED_FILE.read_text().strip().splitlines()
        except Exception:
            note = []
        detail = f': {note[0][:200]}' if note else ''
        return False, f'{LIVE_DISABLED_FILE} exists{detail}'
    setting = os.environ.get(LIVE_ENV_VAR)
    if setting is not None and setting.strip().lower() not in LIVE_ON_VALUES:
        return False, f'{LIVE_ENV_VAR}={setting!r} in the environment'
    return True, ''


# What a domain's optional PACING dict may contain (group 8). Both halves are
# deliberately narrow: create.sh turns these pairs into `export K=V` lines in the env.sh
# that emperor.sh sources as root, so a value is code. Anything with a space, a quote, a
# `$` or a `;` in it is refused rather than quoted -- every setting this is for is a
# number or a duration like `5m`, and a domain that needs shell syntax to express its
# cadence is doing something this hook is not for.
PACING_KEY_RE = re.compile(r'[A-Z][A-Z0-9_]*\Z')
PACING_VALUE_RE = re.compile(r'[A-Za-z0-9_.:+-]+\Z')


def check_pacing(pacing, label='PACING'):
    """Every way `pacing` is unfit to be written into env.sh, as a list of strings.

    Split out of check() because create.sh validates the same dict on the host, read out
    of the domain's *source* (see pacing_from_source) rather than out of an imported
    module -- and a safety check with two implementations is one that will disagree with
    itself about what is safe.
    """
    problems = []
    if not isinstance(pacing, dict):
        return [f'{label} is {type(pacing).__name__}, not a dict']
    for key, value in pacing.items():
        if not isinstance(key, str) or not PACING_KEY_RE.match(key):
            problems.append(f'{label} key {key!r} is not an UPPER_SNAKE env var name')
            continue
        if not isinstance(value, str):
            problems.append(f'{label}[{key!r}] is {type(value).__name__}, not a str '
                            f'(write the number as a string: it becomes an env var)')
        elif not PACING_VALUE_RE.match(value):
            problems.append(f'{label}[{key!r}] = {value!r} is not a bare '
                            f'[A-Za-z0-9_.:+-]+ value safe to write into env.sh')
    return problems


def pacing_from_source(path):
    """The PACING dict declared in the domain module at `path`, or {} if it declares none.

    Parses rather than imports, because the one caller is create.sh, running on the HOST
    before the container exists: importing domain_sdex there would need requests, httpx
    and a /opt that is not mounted yet, and a bootstrap script must not be able to fail on
    a dependency of a module it only wants one constant out of. ast.literal_eval also
    means a domain module cannot compute its pacing at import time, which is the point --
    this is read once, by a shell script, and has to be a constant.

    Raises ValueError if PACING is present but is not a literal, or fails check_pacing.
    """
    tree = ast.parse(open(path).read())
    found = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == 'PACING' for t in node.targets):
            continue
        try:
            found = ast.literal_eval(node.value)
        except (ValueError, SyntaxError) as e:
            raise ValueError(f'{path}: PACING is not a literal dict ({e})')
    if found is None:
        return {}
    problems = check_pacing(found, label=f'{path}: PACING')
    if problems:
        raise ValueError('; '.join(problems))
    return found


# What check() insists on: (member_name, minimum_positional_args) for callables,
# (member_name, None) for plain attributes. Kept as data rather than as a base class so a
# domain stays a plain module -- and so this list reads as the checklist a new domain
# author works through.
CONTRACT = (
    # identity + criterion 6's seed
    ('NAME', None),
    ('TEMPLATE_REPO', None),
    ('STARTING_SCORE', None),
    ('SMOKE_ENV', None),
    # criterion 1
    ('OBSERVE_FAILURE_NOTE', None),
    ('observe', 0),
    ('observe_population', 2),
    ('encode_observation', 1),
    ('decode_observation', 1),
    ('score', 2),
    ('score_path', 2),
    ('activity', 1),
    ('activity_log_path', 1),
    ('config_signature', 1),
    ('RANK_GRACE_S', None),
    # criterion 2
    ('REPLAY_DAYS', None),
    ('REPLAY_WINDOW', None),
    ('replay', 1),
    ('importability', 1),
    # criterion 4
    ('live_enabled', 0),
    ('caps', 0),
    ('can_execute_live', 1),
    ('promotion_sizing', 1),
    ('prepare_live', 1),
    ('retire_live', 1),
    # criterion 6
    ('normalize_config', 2),
    ('sanitize_config', 2),
    ('repair_config', 3),
    ('config_is_sane', 3),
    ('inject_experiments', 2),
    ('seed_config', 3),
    ('tweak_config', 3),
    ('check_replayable', 3),
    ('prepare_smoke_config', 2),
    ('check_smoke_state', 3),
    ('cleanup_scratch', 1),
    # criterion 5's instruments
    ('report_activity', 1),
    ('stuck_report', 3),
    ('report_regime', 1),
    ('report_experiments', 0),
    ('report_live', 1),
    ('ensure_background_jobs', 0),
    ('background_jobs_alive', 0),
    # group 7
    ('prompt_facts', 0),
    ('observation_line', 1),
)

_LOADED = {}


def get(name=None):
    """The domain module for `name` (default: the DOMAIN env var).

    Resolved at the caller's IMPORT time, never mid-cycle. A domain that cannot be
    imported is a total failure of the run and should say so before a cycle starts --
    emperor.sh already notices a monitor that exits after only a few seconds, whereas a
    lazy import that raises in the middle of a cycle takes down scoring, culling and
    spawning for the whole population and reads in the log as one bad cycle.

    Cached because master-agent.py builds its system prompt from prompt_facts() at import
    and monitor calls this once too; re-importing is harmless but the module-level asset
    pool cursors in a domain are per-process state worth keeping single.
    """
    name = name or DOMAIN_NAME
    if name not in _LOADED:
        _LOADED[name] = importlib.import_module(f'domain_{name}')
    return _LOADED[name]


def check(mod):
    """Every way `mod` fails the contract above, as a list of strings. Empty means OK.

    Used by selftest_domain.py against both shipped domains, and it is the first thing a
    new domain should be run through. Arity is checked, not just presence: a member with
    the wrong signature fails at the one call site that uses it, which on this codebase
    can be a path taken once a week.
    """
    problems = []
    for member, min_args in CONTRACT:
        if not hasattr(mod, member):
            problems.append(f'{mod.__name__} is missing {member}')
            continue
        value = getattr(mod, member)
        if min_args is None:
            continue
        if not callable(value):
            problems.append(f'{mod.__name__}.{member} is not callable')
            continue
        try:
            params = inspect.signature(value).parameters.values()
        except (TypeError, ValueError):
            continue        # builtins and C callables have no introspectable signature
        positional = [p for p in params
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        takes_varargs = any(p.kind == p.VAR_POSITIONAL for p in params)
        required = sum(1 for p in positional if p.default is p.empty)
        if not takes_varargs and len(positional) < min_args:
            problems.append(f'{mod.__name__}.{member} takes {len(positional)} positional '
                            f'arg(s), the loop passes {min_args}')
        if required > min_args:
            problems.append(f'{mod.__name__}.{member} requires {required} positional '
                            f'arg(s), the loop passes {min_args}')
    # Group 8, and the only optional member checked here: absent is a legal answer, but a
    # PACING that IS present has to be safe, because create.sh writes it into a file that
    # is sourced as root. Not in CONTRACT for the same reason live_track_record is not --
    # that table is what a domain must have, not what it must not get wrong.
    if hasattr(mod, 'PACING'):
        problems += [f'{mod.__name__}.{p}' for p in check_pacing(mod.PACING)]
    return problems


def first_few(items, limit=6):
    """`limit` names and a count of the rest -- these lists run to 20+ entries."""
    items = sorted(items)
    if len(items) <= limit:
        return ', '.join(items)
    return ', '.join(items[:limit]) + f', and {len(items) - limit} more'


if __name__ == '__main__':
    import sys
    target = get(sys.argv[1] if len(sys.argv) > 1 else None)
    problems = check(target)
    print(f'domain: {target.NAME} ({target.__name__})')
    for problem in problems:
        print(f'  FAIL {problem}')
    print(f'  {len(problems)} contract problem(s)')
    sys.exit(1 if problems else 0)

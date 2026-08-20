#!/usr/bin/env python3
"""The Stellar DEX domain: paper-and-live XLM trading, scored on mark-to-market net worth.

Everything in here used to live in monitor.py. It is domain knowledge -- prices, threshold
bands, spreads, the real-money caps -- and monitor.py is the domain-agnostic evolutionary
loop. See domain.py for the contract this implements and for which of FUTURE.md's criteria
each member answers.

XLM ONLY, since 2026-08-13. This domain used to trade XLM plus up to two discovered
Stellar DEX assets per strategy, and roughly a third of this file was the machinery for
that: an injection channel, an admission registry, per-leg threshold bands, trustline
opening at promotion, and a config `assets` array carried down every lineage. It was
removed because the population had stopped using it -- 13 of 79 tracked strategies still
declared a leg, 1 running, and the last non-XLM fill of any kind was seven days before
the removal, 621 of 18,151 lifetime fills. The verification stack it fed
(tools/asset_discovery.py, tools/dex_price.py, and stellar_trader's non-base caps and
trustline handling) was deliberately left in place: it is the money boundary, its refusal
paths are now simply unreachable, and deleting them would buy nothing.

This is a MOVE, not a rewrite. The docstrings and comments came across verbatim because
they are the record of specific past failures (a haircut two orders of magnitude off the
prompt that quoted it; 141 configs with no schema_version; a Reflector channel with a 0%
admission rate; a +/-2% seed band that fired zero times in thirty days) and they are the
primary input to the next emperor self-revision pass. If you change behaviour in here,
add to the record -- do not trim it.
"""
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import domain
import score as _score


def _strategy_python():
    """One definition of "which python runs a strategy", shared with the process that
    actually starts them, so a supervised daemon can never run under a different
    interpreter than the strategies do.

    Imported at CALL time, not module scope: strat_manager.py mkdirs /opt/strategies as an
    import side effect, which makes it unimportable anywhere but inside the container as
    root -- and that would make this whole module, and therefore the contract check and the
    self-test, container-only for no reason. Resolution still happens in exactly one place.
    """
    from strat_manager import _strategy_python as resolve
    return resolve()


NAME = 'sdex'

TEMPLATE_REPO = 'file:///opt/template_repo'

# What an untouched strategy scores: template_repo/main.py starts every state.json at
# $1000 cash and no XLM. Used to seed a template spawn's "parent score" and as the
# baseline the turnover report measures gain against.
STARTING_SCORE = 1000.0

# How long a strategy is exempt from the rank-KEEP_TOP_N cull after it starts acting (see
# domain.py's criterion 1 docstring for why this moved from monitor.py's old bare
# YOUNG_GRACE_S constant to a per-domain value). SDEX marks move every cycle, so three
# cycles' worth of real trading is already enough evidence to rank on -- unchanged from
# the constant every domain shared before this became domain-owned.
RANK_GRACE_S = 3 * 3600

# The environment the smoke-test harness runs candidate code under. PAPER_ONLY makes the
# no-live-trading guarantee structural rather than relying on having remembered to drop
# live.flag from the copy: stellar_trader._paper_only() refuses inside submit_trade, not
# at import, so no code path can talk itself past it.
SMOKE_ENV = {'PAPER_ONLY': '1'}

STRATEGIES_DIR = domain.STRATEGIES_DIR
TRADES_DIR = domain.TRADES_DIR


# --------------------------------------------------------------------------------------
# Criterion 1: observation. One reading of the world per cycle.
# --------------------------------------------------------------------------------------

PRICE_FETCH_ATTEMPTS = 3
PRICE_RETRY_DELAY = 60      # seconds between price-fetch attempts

OBSERVE_FAILURE_NOTE = f'Could not fetch price after {PRICE_FETCH_ATTEMPTS} attempts'


@dataclass
class Observation:
    """What this domain knows at the top of a cycle.

    `price` is XLM/USD from price_feed. `marks` maps spec -> USD unit price and now only
    ever holds the one XLM entry; it stays a mapping because score.py, portfolio.net_worth
    and check_smoke_state all take that shape, and because collapsing it would be a
    signature change across four modules to save one dict.

    Opaque to monitor.py by design: the loop only ever tests `is None` and passes it back.
    A domain with no prices returns whatever shape it likes from observe().
    """
    price: float
    marks: dict = field(default_factory=dict)


def _get_current_price():
    # Use the shared price_feed tool
    import sys as _sys
    _sys.path.append('/opt/tools')
    from price_feed import get_price
    return get_price()


def observe():
    """One XLM/USD price, or None if every source failed. Retries before giving up.

    price_feed already tries 6 sources, but they all share one network path: when the
    container's DNS flapped on 2026-08-01 every source failed at once. Retrying a few
    times beats throwing away a whole hour of trading on a transient outage.
    """
    for attempt in range(1, PRICE_FETCH_ATTEMPTS + 1):
        price = _get_current_price()
        if price is not None:
            return Observation(price=price)
        if attempt < PRICE_FETCH_ATTEMPTS:
            print(f'Price fetch failed (attempt {attempt}/{PRICE_FETCH_ATTEMPTS}); '
                  f'retrying in {PRICE_RETRY_DELAY}s')
            time.sleep(PRICE_RETRY_DELAY)
    return None


def observe_population(obs, state):
    """The cycle's marks. XLM only -- this domain trades nothing else.

    Kept as a contract member (and `marks` kept as a dict rather than collapsed into
    `price`) because score.py, check_smoke_state and the harness all take a spec->mark
    mapping, and because a future domain may well need a real batch here. It no longer
    does any network I/O: with the extra-asset stack gone the only mark that exists is
    the XLM price observe() already fetched.
    """
    obs.marks = {'XLM': obs.price}
    return obs


def encode_observation(obs):
    """The observation as one argv token for the revise-strategy subprocess.

    Deliberately `str(price)` and nothing else: that is byte-for-byte what monitor.py
    passed as argv 5 before this boundary existed, so a hand-typed
    `master-agent.py revise-strategy <name> <parent> <score> <leaderboard> 0.1666`
    still works, and the rendered prompt is unchanged. `marks` is not included because
    the revision prompt never used it.
    """
    return str(obs.price)


def decode_observation(text):
    """The other side of encode_observation. None if there is nothing usable.

    Accepts a bare float (what encode_observation emits, and what a human types) or a
    JSON object, so a future structured observation needs no second argv change. None is
    a legitimate value, not an error: monitor may fail to fetch a price and the revision
    prompt has a branch for exactly that -- see observation_line.
    """
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    try:
        return Observation(price=float(text))
    except ValueError:
        pass
    try:
        data = json.loads(text)
        return Observation(price=float(data['price']), marks=data.get('marks') or {})
    except Exception:
        return None


def observation_line(obs):
    """How the revision prompt states the current price. See domain.py group 7.

    The "this is ground truth" framing is load-bearing: the model otherwise anchors on a
    price from its training data and writes a threshold band the market has not visited in
    months, which _thresholds_are_sane then rejects and repair_config has to rebuild.
    """
    return (
        f'Current XLM/USD price (fetched from CoinGecko by monitor.py moments ago, '
        f'this is ground truth -- not a historical or typical price): ${obs.price}\n'
        if obs is not None else
        'Current XLM/USD price: NOT PROVIDED for this cycle -- fetch it yourself '
        '(exec curl, or /opt/tools/price_feed.py) before setting any thresholds.\n'
    )


# --------------------------------------------------------------------------------------
# Criterion 1: fitness. Mark-to-market net worth, via score.py.
# --------------------------------------------------------------------------------------

def score(state_dict, obs):
    """(score, unpriced_specs) for one strategy's state. See score.py for the haircuts."""
    marks = dict(obs.marks or {})
    marks.setdefault('XLM', obs.price)
    return _score.compute_score_multi(state_dict, marks)


def score_path(strategy_path, obs):
    """Read <strategy_path>/state.json and return its score, or None if unreadable."""
    return _score.score_from_strategy_path(strategy_path, obs.price, obs.marks or None)


def activity_log_path(name):
    """Where `name`'s trades actually got logged.

    Normally /opt/trades/<name>.log, but trade_logger names the file after config.json's
    "name" field, which a revision is free to rewrite -- clone_916b729411ab logs to
    clone_916b729411ab_modified.log for exactly that reason. Fall back to whatever the
    config says so a renamed strategy doesn't look like it has never traded.
    """
    log_path = TRADES_DIR / f'{name}.log'
    if log_path.exists():
        return log_path
    try:
        entry = json.load(domain.STATE_FILE.open()).get(name)
        cfg = json.load(open(Path(entry['path']) / 'config.json'))
        alt = TRADES_DIR / f"{cfg['name']}.log"
        if alt.exists():
            return alt
    except Exception:
        pass
    return log_path


def activity(name):
    """(trade_count, first_timestamp, last_timestamp) from this strategy's trade log.

    Used both to break ranking ties and to decide whether a strategy has enough of a
    track record to be trusted with the live pubnet flag. Returns zeros if the strategy
    has never traded, which is the common case for a fresh clone.
    """
    log_path = activity_log_path(name)
    if not log_path.exists():
        return 0, 0.0, 0.0
    first = last = 0.0
    count = 0
    try:
        with log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                count += 1
                try:
                    ts = json.loads(line).get('timestamp', 0.0)
                except Exception:
                    continue
                if not first:
                    first = ts
                last = ts
    except Exception as e:
        print(f'Could not read trade log for {name}: {e}')
    return count, first, last


def turnover_stats(name):
    """(trades, turnover_usd, friction_usd) from this strategy's trade log.

    Turnover is the sum of every fill's notional -- the quantity a spread is charged on,
    and the one number that says whether a strategy has an edge or just a habit. On
    2026-08-03 the leader had turned over $5,757 to gain $23.33: a 40.5 bp edge, against
    a book that costs ~8.7 bp round trip and extra-asset books that cost 150-190. That
    ratio existed in the trade logs all along and nothing ever computed it, so "962
    trades" read as evidence of a working strategy rather than as a cost to be covered.

    `friction_usd` is summed from the per-line friction_bp that trade_logger started
    writing on 2026-08-03. Lines from before that are counted into turnover but
    contribute no cost, which is correct -- they genuinely were not charged any.
    """
    log_path = activity_log_path(name)
    if not log_path.exists():
        return 0, 0.0, 0.0
    trades = 0
    turnover = friction_usd = 0.0
    try:
        with log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                trades += 1
                notional = abs(float(row.get('amount_usd') or 0.0))
                turnover += notional
                bp = row.get('friction_bp')
                if bp:
                    friction_usd += notional * float(bp) / 10000.0
    except Exception as e:
        print(f'Could not read trade log for {name}: {e}')
    return trades, turnover, friction_usd


def config_signature(state_entry):
    """The parameters that actually define a strategy's behaviour, for dedup."""
    try:
        cfg = json.load(open(Path(state_entry['path']) / 'config.json'))
    except Exception:
        return None
    # basis_min_bp is here because it is the entire difference between the gated arm and
    # the control arm: without it two spawns that differ only in whether they trade on
    # the basis look identical, select_parents drops one as a duplicate, and the
    # experiment loses half its population to a dedup rule. It is now the only such knob
    # -- the `assets` tuple that used to sit beside it went with the extra-asset stack.
    return (cfg.get('buy_below'), cfg.get('sell_above'),
            cfg.get('trade_amount_usd'), cfg.get('basis_min_bp'))


# --------------------------------------------------------------------------------------
# Shared lazy handles on /opt/tools.
# --------------------------------------------------------------------------------------

def _tools():
    """Lazy handle on /opt/tools' portfolio module, or None if unavailable.

    Imported lazily and defensively so that a problem in the shared tools degrades the
    one caller left (check_smoke_state) to its own reduced check instead of taking down
    the whole culling loop. It used to return dex_price and asset_discovery alongside
    this; both went with the extra-asset stack.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import portfolio
        return portfolio
    except Exception as e:
        print(f'portfolio tooling unavailable ({e}); using reduced checks this cycle')
        return None


# --------------------------------------------------------------------------------------
# Criterion 4: the real-money boundary.
# --------------------------------------------------------------------------------------

def live_enabled():
    """May this domain keep ANY strategy live right now? (enabled, reason). FAILS CLOSED.

    The coarse switch the loop asks once per cycle before it promotes anything;
    can_execute_live is the per-strategy one, and neither substitutes for the other (see
    domain.py's criterion 4). Four ways to say no, none of them reachable by a revision:

      * the operator's generic switch -- domain.live_switch(), i.e. /opt/.live_disabled
        or LIVE_TRADING=off on the monitor process;
      * PAPER_ONLY in this process's environment, which every strategy monitor starts
        inherits and which stellar_trader.submit_trade refuses on structurally. A monitor
        running paper-only that still promoted, wrote live.flag and recorded promotion
        sizing was reporting a live strategy that could not place an order;
      * stellar_trader's own /opt/trades/.live_halt -- what already refuses every real
        order, and what _mark_stuck writes automatically past MAX_STUCK_USD. Surfacing it
        to the loop is the point: it used to stop the money without stopping the loop, so
        monitor kept promoting and kept believing money was deployed behind positions
        that were never opened, which is the exact divergence live_report.py had to be
        built to notice after 343 consecutive refused orders went unremarked;
      * stellar_trader not importing at all -- the money boundary is not present to
        enforce anything, so nothing may be live.

    _HALT_PATH is read off the module rather than restated as a literal here. That
    constant belongs to the money boundary; a second copy on this side is a copy that
    eventually disagrees with the one doing the refusing.
    """
    enabled, reason = domain.live_switch()
    if not enabled:
        return False, reason
    if os.environ.get('PAPER_ONLY'):
        return False, 'PAPER_ONLY is set on this process'
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import stellar_trader
    except Exception as e:
        return False, f'could not import stellar_trader to check its halt switch ({e})'
    try:
        halted = stellar_trader._HALT_PATH.exists()
    except Exception as e:
        return False, f'could not check the stellar_trader halt switch ({e})'
    if halted:
        return False, f'{stellar_trader._HALT_PATH} exists (stellar_trader kill switch)'
    return True, ''


def caps():
    """stellar_trader's current caps, or None. Never raises.

    Lazy and defensive for the same reasons _tools() is. Only ever used to *record* what
    the caps were when a strategy was promoted -- a strategy earns the live flag on a
    paper track record sized by its own config.json, then trades real money clamped by
    these, and the two numbers are not the same. Failing to read them must degrade that
    record, never block a promotion.

    Only the two XLM caps. stellar_trader still defines the non-base ones and still
    enforces them -- that module is the money boundary and was deliberately left alone --
    but with no strategy able to declare an extra asset they can no longer bind on
    anything, and recording them here put dead numbers into every live_strategy.json.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import stellar_trader
        return {'max_trade_usd': float(stellar_trader.MAX_TRADE_USD),
                'max_daily_usd': float(stellar_trader.MAX_DAILY_USD)}
    except Exception as e:
        print(f'could not read stellar_trader caps ({e}); recording sizing without them')
        return None


def can_execute_live(name):
    """Whether <name>/main.py routes its trades through trade_logger.execute_trade.

    About ten strategies in the population still mutate balances inline and call
    record_trade directly -- the style the template moved away from. They log paper
    trades normally and are indistinguishable on the leaderboard, but execute_trade is
    the *only* path that checks live.flag and calls stellar_trader.submit_trade, so
    those strategies are structurally incapable of placing a real order.

    Promoting one is worse than a no-op: set_live_flag writes live.flag, the strategy
    ignores it, and monitor now believes real money is deployed behind a position that
    was never opened. The next leader change then calls wind_down() against an account
    this strategy never touched, and gates the promotion on liquidating a position that
    doesn't exist.

    Returns (ok, reason). An unreadable or unparseable main.py fails closed.

    A second, independent check runs once the AST check passes (SHORTING_PLAN.md): a
    strategy whose config.json sets allow_shorting must also have a real, funded short
    buffer before it may go live -- the config flag is a paper-side capability switch,
    not proof the money backing it exists. See _can_short_live.
    """
    import ast
    main_py = STRATEGIES_DIR / name / 'main.py'
    try:
        tree = ast.parse(main_py.read_text())
    except Exception as e:
        return False, f'could not parse main.py ({e})'
    calls_execute_trade = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
        if called == 'execute_trade':
            calls_execute_trade = True
            break
    if not calls_execute_trade:
        return False, ('main.py never calls execute_trade (inline record_trade style), '
                       'so it cannot submit a real order')
    return _can_short_live(name)


def _can_short_live(name):
    """Second half of can_execute_live's gate (SHORTING_PLAN.md), only reached once the
    AST check above passes.

    Not gated by anything a revision can flip on its own: `allow_shorting` lives in
    config.json, which a revision can set, but the buffer it checks for is a real
    deposit recorded by a human via stellar_trader's _SHORT_BUFFER_PATH marker file, and
    the balance check hits Horizon directly. Setting the config flag alone cannot make
    this pass. Fails closed on any read/network error, same contract as
    can_execute_live itself (see domain.py) -- this guards real money, not a fitness
    signal, and `--force` deliberately cannot skip it (only qualifies_for_live's
    track-record bar).
    """
    try:
        cfg = json.load(open(STRATEGIES_DIR / name / 'config.json'))
    except Exception as e:
        return False, f'could not read config.json ({e})'
    if not cfg.get('allow_shorting'):
        return True, ''
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import stellar_trader
    except Exception as e:
        return False, f'could not import stellar_trader to verify short buffer ({e})'
    try:
        status = stellar_trader.short_buffer_status()
    except Exception as e:
        return False, f'could not verify short buffer ({e})'
    if not status['funded']:
        return False, f'allow_shorting is set but short buffer is not ready: {status["reason"]}'
    return True, ''


def promotion_sizing(name):
    """The paper trade size this strategy was promoted on, vs the real per-trade cap.

    Recorded at promotion because the two are not the same number and nothing used to
    say so: a strategy earns the live flag on a paper track record of trade_amount_usd
    fills, then trades real money clamped by stellar_trader's caps, so the live P&L is
    not a scaled copy of the paper P&L that justified the promotion. This is only the
    *configured expectation* -- the realized ratio is per-trade and lower whenever the
    daily budget or claudio's balance binds, which is why trade_logger records it per
    trade and live_report reads it back. None on any failure; this must never block a
    promotion.
    """
    try:
        cfg = json.load(open(STRATEGIES_DIR / name / 'config.json'))
        sizing = {'trade_amount_usd': float(cfg.get('trade_amount_usd') or 0.0)}
        limits = caps()
        if limits:
            sizing.update(limits)
            if sizing['trade_amount_usd'] > 0 and limits['max_trade_usd'] > 0:
                sizing['implied_ratio'] = round(
                    min(1.0, limits['max_trade_usd'] / sizing['trade_amount_usd']), 6)
        return sizing
    except Exception as e:
        print(f'could not record promotion sizing for {name} ({e})')
        return None


def prepare_live(name):
    """Nothing to prepare in this domain any more. Always {}.

    This used to open one Stellar trustline per declared extra asset, called at promotion
    between the outgoing strategy's wind_down and the incoming one's live.flag. A strategy
    can no longer declare an extra asset, so there is never a trustline to open: every
    live strategy trades the native XLM leg, which needs none.

    Kept rather than deleted because it is a DOMAIN contract member (domain.check pins the
    name and arity) and because the ordering it occupies in monitor's promotion sequence is
    load-bearing for any domain that does need setup there. stellar_trader.ensure_trustline
    survives untouched on the other side of the money boundary and is once again callerless,
    which is what it was before this loop existed.
    """
    return {}


def ensure_trading_cushion(name):
    """Top up claudio's real spendable XLM before `name` starts trading real money.

    Called once at promotion, after live.flag and live_strategy.json both already point
    at `name` -- ordering matters here, see stellar_trader.ensure_trading_cushion's
    docstring for why. This is about the native XLM leg every strategy trades, which is
    now the only leg there is.

    A failure is NOT fatal, same contract as prepare_live: a strategy that goes live with
    a thin real balance just means more early sell refusals against
    MIN_TRUSTLINE_RESERVE_XLM, not a broken promotion.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import stellar_trader
    except Exception as e:
        print(f'stellar_trader unavailable ({e}); skipping trading cushion for {name}')
        return

    try:
        result = stellar_trader.ensure_trading_cushion()
    except Exception as e:
        print(f'could not fund trading cushion for {name}: {e}')
        return

    topped = result.get('topped_up_usd', 0.0)
    if topped:
        print(f'  funded ${topped:.2f} of trading cushion for {name} '
              f'({result.get("chunks")} chunk(s))')
    elif result.get('reason'):
        print(f'  trading cushion for {name} not topped up: {result["reason"]}')


def retire_live(old_name):
    """Flatten the outgoing live strategy's real position. Returns (ok, [lines to print]).

    A leader change is gated on this succeeding: if wind_down cannot fully liquidate in
    one cycle the old leader simply stays live and monitor retries next cycle. That is
    safe -- the cull exempts the live strategy from KEEP_TOP_N -- and it is why this
    returns a verdict rather than raising.

    Lines are returned rather than printed so the loop owns the cycle log's ordering, but
    their text (including the leading indentation) is the domain's: they name XLM, chunks
    and stuck legs.
    """
    lines = []
    try:
        import sys as _sys
        _sys.path.append('/opt/tools')
        from stellar_trader import wind_down
        result = wind_down()
    except Exception as e:
        # Fails CLOSED, unlike the fitness gates: not being able to confirm the outgoing
        # strategy is flat is exactly the case where promoting anyway would leave real
        # money behind a strategy nothing is watching.
        return False, [f'wind_down unavailable ({e}); {old_name} stays live, '
                       f'retrying next cycle']
    if not result['liquidated']:
        lines.append(f"wind_down did not fully flatten {old_name} this cycle "
                     f"(remaining_xlm={result['remaining_xlm']}, reason={result['reason']}); "
                     f"{old_name} stays live, retrying next cycle")
        return False, lines
    lines.append(f"wind_down flattened {old_name} ({result['chunks']} chunk(s))")
    if result.get('stuck'):
        # Computed by wind_down and, until now, thrown away here. A stuck leg denies
        # that asset permanently, suspends every non-XLM buy system-wide, and counts
        # toward the MAX_STUCK_USD halt -- so it silently degrades the strategy being
        # promoted right now, and the only trace was a dotfile nobody reads.
        lines.append(f"  WARNING: {len(result['stuck'])} leg(s) left stuck and unsellable: "
                     f"{', '.join(result['stuck'])}")
        lines.append(f"  non-XLM buys are suspended system-wide until "
                     f"/opt/trades/.stuck_positions.json is cleared by a human")
    return True, lines


# --------------------------------------------------------------------------------------
# Criterion 2: replay.
# --------------------------------------------------------------------------------------

REPLAY_DAYS = 30
REPLAY_WINDOW = f'{REPLAY_DAYS}d of real candles'


def importability(source_or_path):
    """(ok, reason) from backtest.importability_report, or None if it can't be consulted.

    None means "cannot tell", and every caller FAILS OPEN on it. That is deliberately the
    opposite of can_execute_live, which fails closed: that one guards real money, this one
    guards a *fitness signal*. Failing closed whenever /opt/tools were missing or
    mid-rewrite would revert every revised main.py in the population, freeze main.py
    evolution entirely and route every clone to tweak_config -- a much larger and much
    quieter failure than the blindness it is trying to prevent, and one that would read in
    the logs as a run of bad revisions.

    Also tolerates a /opt/tools that predates importability_report: emperor.sh rewrites
    the two repos independently, so version skew between them is a real state, not a
    hypothetical.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import backtest
        report = getattr(backtest, 'importability_report', None)
        if report is not None:
            return report(source_or_path)
        ok = backtest._is_importable(source_or_path)
        return ok, ('decide() is importable' if ok
                    else 'backtest cannot import a top-level decide() from main.py')
    except Exception as e:
        print(f'importability check unavailable ({e}); not gating on it')
        return None


def replay(strategy_dir):
    """Replay this strategy over REPLAY_DAYS of real candles, or None if unmeasurable.

    Returns {'trades', 'beats_null', 'null_pct', 'raw'}. None means "could not be
    measured" and every caller fails open on it, for the same reason importability() does:
    this guards a fitness signal, not money, and a /opt/tools that is missing or
    mid-rewrite must not start reverting every revision in the population.

    Why the loop gates on `trades` at all: on 2026-08-05 the revision model shipped
    configs whose band did not intersect the market, then justified them from a backtest
    that reported `trades: 0` alongside `beats_buy_hold: true` -- which is not a
    contradiction, because XLM was falling and doing nothing beat holding it. The log has
    the model saying so in as many words ("Trades executed 0 times, which is expected ...
    however, the beats_buy_hold flag returned true"). `beats_buy_hold` on its own
    therefore accepts a strategy that will never place an order; the trade count is the
    missing half of that test.

    `beats_null` is criterion 3 -- for this domain, buy-and-hold. Carried through so the
    signal has a home even though the loop does not currently gate on it.

    Costs about a second per newcomer against SMOKE_TEST_SECONDS (120) already spent, and
    only runs for newcomers, so at most three times a cycle.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import backtest
        result = backtest.backtest(str(strategy_dir), days=REPLAY_DAYS, interval=60)
        return {'trades': int(result.get('trades', 0)),
                'beats_null': result.get('beats_buy_hold'),
                'null_pct': result.get('buy_hold_pct'),
                'raw': result}
    except Exception as e:
        print(f'  backtest trade count unavailable ({e}); not gating on it')
        return None


# --------------------------------------------------------------------------------------
# Criterion 5's instruments, and the background writer they depend on.
# --------------------------------------------------------------------------------------

# The market recorder. It used to be one snapshot per cycle taken inline in run(), which
# was fine while nothing read the rows back. The DEX/CEX basis changed that: it is a
# per-tick input, and dex_price.get_orderbook is uncached, so every strategy measuring
# its own would be ~40 Horizon order-book GETs a minute. One supervised writer at 60s
# feeds all of them through /opt/trades/.market_history.jsonl instead. The daemon
# deliberately outlives a single cycle -- a per-cycle process would leave gaps exactly
# as long as a revision takes.
RECORDER_SCRIPT = Path('/opt/tools/market_recorder.py')
RECORDER_INTERVAL = 60
RECORDER_PID_FILE = TRADES_DIR / '.market_recorder.pid'
RECORDER_LOG = TRADES_DIR / 'market_recorder.log'


def background_jobs_alive():
    """Is the market recorder daemon running? Pid file plus two confirmations.

    os.kill(pid, 0) alone is not enough. This pid file survives a container restart,
    and pids are recycled, so a stale file can name a live and entirely unrelated
    process -- at which point monitor would believe it had a recorder forever and every
    basis reader would silently degrade to neutral. /proc/<pid>/cmdline settles it.
    """
    try:
        pid = int(RECORDER_PID_FILE.read_text().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    try:
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes().decode('utf-8', 'replace')
    except Exception:
        return False    # cannot confirm -> assume not ours and respawn
    return 'market_recorder' in cmdline


def ensure_background_jobs():
    """Start the market recorder daemon if it is not already running, then report.

    Idempotent, and called once per cycle: the daemon outlives a cycle (and an emperor
    window, since it is setsid'd out of monitor's process group), so the common path
    here is the background_jobs_alive() check and nothing else.

    _strategy_python(), never a bare python3. /usr/bin/python3 cannot import the
    third-party packages in /opt/agents/venv, and a bare python3 is exactly what made
    every revision fail silently for weeks -- the same trap, one directory over.
    """
    if not background_jobs_alive():
        TRADES_DIR.mkdir(parents=True, exist_ok=True)
        log = open(RECORDER_LOG, 'a')
        proc = subprocess.Popen(
            [_strategy_python(), '-u', str(RECORDER_SCRIPT),
             '--daemon', '--interval', str(RECORDER_INTERVAL)],
            stdout=log, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,   # survives a TERM to monitor's process group
        )
        RECORDER_PID_FILE.write_text(str(proc.pid))
        print(f'Started market recorder (pid {proc.pid}, every {RECORDER_INTERVAL}s) '
              f'-> {RECORDER_LOG}')

    if '/opt/tools' not in sys.path:
        sys.path.append('/opt/tools')
    import market_recorder
    span = market_recorder.span()
    # A cycle in which the recorder was just started legitimately has no fresh row yet;
    # one in which it has been up for hours and still has none is the failure that would
    # otherwise be invisible, because every basis reader degrades to neutral in silence.
    row = market_recorder.tail(1)
    last = row[0] if row else {}
    age = round(time.time() - last['ts']) if last.get('ts') else None
    print(f"Market history: {span['rows']} rows over {span['hours']}h; "
          f"last row {age}s ago, basis {last.get('basis_bp')} bp, "
          f"tradeable {last.get('tradeable_bp')} bp, spread {last.get('spread_bp')} bp")
    if age is not None and age > RECORDER_INTERVAL * 5:
        print(f'WARNING: market history is {age}s stale; every basis reader is '
              f'degrading to neutral. Check {RECORDER_LOG}')


def report_activity(performances, limit=None):
    """One block per cycle: what the leaders traded, and what trading it cost them.

    Sits next to the score table deliberately. Score says who is winning; this says
    whether they are winning by having an edge or by being lucky with a lot of coin
    flips, which is the distinction the cull could not previously make.

    This is the instrument for FUTURE.md's criterion 5 -- "enough independent bets per
    hour that ranking isn't noise" -- which is the criterion the current system fails.
    """
    rows = []
    for name, strategy_score in performances[:limit]:
        trades, turnover, friction_usd = turnover_stats(name)
        if not trades:
            continue
        gain = strategy_score - STARTING_SCORE      # vs the 1000.00 starting balance
        # Edge per dollar traded. Below the round-trip cost of the book it trades on,
        # a strategy is paying to play.
        edge_bp = (gain / turnover * 10000) if turnover > 0 else None
        rows.append((name, trades, turnover, gain, edge_bp, friction_usd))
    if not rows:
        return
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import friction
        cost_bp = friction.round_trip_bp('XLM')
    except Exception:
        cost_bp = None
    header = f'Turnover vs edge (XLM round trip costs {cost_bp} bp):' if cost_bp \
        else 'Turnover vs edge:'
    print(header)
    for name, trades, turnover, gain, edge_bp, friction_usd in rows:
        edge = f'{edge_bp:.1f} bp/$' if edge_bp is not None else 'n/a'
        # The verdict, spelled out: an edge thinner than the toll is not an edge.
        verdict = ''
        if edge_bp is not None and cost_bp:
            verdict = '  <-- edge below trading cost' if edge_bp < cost_bp else ''
        print(f'  {name}: {trades} trades, ${turnover:,.0f} turnover, '
              f'{gain:+.2f} gain, {edge}, ${friction_usd:.2f} paid{verdict}')


def stuck_report(performances, state, obs):
    """The ALL-IN half of the who-can-still-act report, or None. See monitor's idle half.

    ALL-IN: balance_usd is 0.00 and sell_above is above the market, so there is no buy it
    can afford and no sell it can reach. 25 of 32 were in this state on 2026-08-05,
    holding ~5,960 XLM each. They are not strategies at that point, they are one leveraged
    opinion on XLM, which is why the whole population's scores move together.

    Reported rather than acted on. An all-in strategy is a legitimate (if unlucky)
    position and monitor has no business overriding it -- but the count belongs in the
    cycle log, because "the entire population is long and cannot sell" is the kind of
    thing that is obvious in one number and invisible in thirty scores.
    """
    price = obs.price if obs is not None else None
    allin = []
    for name, _score_value in performances:
        entry = state.get(name)
        if not entry:
            continue
        try:
            st = json.load(open(Path(entry['path']) / 'state.json'))
            cfg = json.load(open(Path(entry['path']) / 'config.json'))
        except Exception:
            continue
        # 1 cent rather than exactly 0: trade_logger leaves rounding dust behind, and a
        # strategy with $0.004 of cash is just as unable to buy as one with none.
        if float(st.get('balance_usd', 0.0)) < 0.01:
            try:
                sell_above = float(cfg['sell_above'])
            except (KeyError, TypeError, ValueError):
                continue
            if price and sell_above > price:
                allin.append(f'{name} (needs {sell_above:g}, spot {price:g})')
    if not allin:
        return None
    return (f'All-in: {len(allin)} of {len(performances)} strategies hold no USD and sit '
            f'below their sell_above, so they can neither buy nor sell: '
            f'{domain.first_few(allin)}')


def report_regime(obs):
    """What market the whole population is trading in. One line, from real candles.

    Trend, volatility against the round-trip cost, and where spot sits in its 30-day
    range. Every strategy here is long-only, so "XLM fell 15.8% this window" is most of
    the explanation for any cycle where everything scores below 1000, and it was
    previously nowhere in the log.
    """
    if '/opt/tools' not in sys.path:
        sys.path.append('/opt/tools')
    import regime
    print(regime.summary())


def report_experiments():
    """The gated-vs-control readout for the basis arm, in the log the emperor pass reads.

    Net worth cannot answer this question in any reasonable time -- it is one noisy sample
    an hour -- while realized venue edge is one observation per fill.
    """
    # basis_report is NOT among the modules symlinked into /opt beside monitor.py
    # (score, strat_manager, leaderboard, live_report), which looks like it should break
    # a symlink-launched monitor and does not: CPython resolves the symlink when it sets
    # sys.path[0], so /opt/master_agent is already there. This append is belt and braces,
    # not a repair -- it keeps the import working if monitor is copied rather than
    # symlinked, or run under an interpreter that leaves sys.path[0] unresolved.
    _ma = os.path.dirname(os.path.realpath(__file__))
    if _ma not in sys.path:
        sys.path.append(_ma)
    import basis_report
    print(basis_report.summary_line(24))


def report_live(live_name):
    """One line on how the live strategy's real fills compare to the paper book it is
    ranked on.

    emperor_logs are the primary input to the next emperor pass, and without this the
    divergence exists only in the strategy's own stdout -- which is where 343 consecutive
    refused orders sat unnoticed on 2026-08-03.
    """
    import live_report
    line = live_report.summary_line(live_name)
    if line:
        print(line)


# --------------------------------------------------------------------------------------
# Criterion 6: synthesizing, repairing and judging a genome.
# --------------------------------------------------------------------------------------

# Odds that a template spawn is seeded with a `basis_min_bp` gate, and the percentile of
# the recorded tradeable_bp distribution used as its threshold. A coin flip rather than
# always: the un-seeded spawns are the control arm that basis_report.py compares the
# seeded ones against. See _inject_basis_gate. Since the extra-asset channel was removed
# this is the only mechanical source of novelty the population cannot invent for itself.
BASIS_INJECT_CHANCE = 0.5
BASIS_INJECT_PERCENTILE = 0.25
BASIS_MIN_RECORDED_HOURS = 6

BASIS_GATE_MAX_PERCENTILE = 0.95

SEED_BAND_FALLBACK_HALF_BP = 200.0   # the old hardcoded price*0.98 / price*1.02


def normalize_config(cfg_path, name):
    """Fill the three required keys monitor always knows the right value for.

    _required_keys_are_sane demands name, schema_version and trade_amount_usd. On
    2026-08-04, 101 of 141 strategy configs had no schema_version at all -- it predates
    them -- so requiring it without this would reject most revised clones over a key
    their parent never had, discard the model's work, and fall through to a random
    tweak. Rejecting is only the right answer when monitor cannot know the correct
    value; for these three it always can, so it supplies them instead.

    ABSENT KEYS ONLY. This deliberately does not correct a key that is present but
    wrong. A `name` naming some other strategy means the model copied a whole
    config.json from somewhere -- most likely the parent's, which the refine prompt
    hands it verbatim -- and that is a revision to reject, not to patch: whatever else
    it copied is suspect too. Overwriting it here would make the name check dead code.

    Returns True if it rewrote the file.
    """
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return False        # unparseable; config_is_sane will fail it and the
                            # fallback rebuilds the file from scratch
    if not isinstance(cfg, dict):
        return False

    filled = {}
    if 'name' not in cfg:
        filled['name'] = name
    if 'schema_version' not in cfg:
        filled['schema_version'] = 2
    if 'trade_amount_usd' not in cfg:
        filled['trade_amount_usd'] = 10.0
    if not filled:
        return False

    cfg.update(filled)
    try:
        json.dump(cfg, open(cfg_path, 'w'), indent=2)
    except Exception as e:
        print(f'  could not normalize config for {name}: {e}')
        return False
    print(f'  filled missing config keys for {name}: '
          f'{", ".join(f"{k}={v!r}" for k, v in filled.items())}')
    return True


def sanitize_config(cfg_path, obs=None):
    """Strip the retired `assets` key off a config. Returns [] always.

    This used to re-verify every declared extra asset against asset_discovery and delete
    the ones that failed. Extra assets are gone, so the only job left is migration: the
    key is committed to git, so it propagates through `git clone` down an entire lineage
    and would otherwise sit in config.json forever looking meaningful. Roughly a dozen
    strategies in the population still carry one; each gets cleaned the next time it is
    provisioned.

    Still called unconditionally on every clone, revised or not, for exactly the reason it
    always was -- an inherited key has to be removed where it is inherited, not where it
    was written. Returns a list because DOMAIN.sanitize_config's contract says so and
    monitor discards the value; there is nothing left for it to describe.
    """
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return []
    if not isinstance(cfg, dict) or 'assets' not in cfg:
        return []
    dropped = cfg.pop('assets')
    try:
        json.dump(cfg, open(cfg_path, 'w'), indent=2)
    except Exception as e:
        print(f'  could not rewrite config after dropping assets: {e}')
        return []
    if isinstance(dropped, list) and dropped:
        codes = ', '.join(str(a.get('code')) for a in dropped if isinstance(a, dict))
        print(f'  dropped retired `assets` key ({codes or len(dropped)}): '
              f'this domain trades XLM only')
    else:
        print('  dropped retired `assets` key (empty)')
    return []


def _tradeable_bp_values(hours=72):
    """The recorded XLM tradeable_bp series, sorted ascending, or None if unreadable.

    One reader for the three places that size a decision against this distribution:
    _inject_basis_gate picks a threshold out of it, _basis_gate_is_sane rejects one above
    it, and repair_config clamps one back into it. They have to agree -- a gate rejected
    at a percentile computed over a different window than the one it was injected from
    would eventually reject monitor's own injected value.

    spec='XLM' explicitly: the gate is on the XLM leg, and the recorder can be pointed at
    another spec, at which point an unfiltered series would mix two assets' dislocations
    into one percentile. Returns None rather than raising -- every caller is on a path
    that must not take the cycle down, and each decides for itself what missing history
    means (inject refuses, the other two fail open).
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import market_recorder
        return sorted(v for v in market_recorder.series('tradeable_bp', hours=hours,
                                                        spec='XLM') if v is not None)
    except Exception:
        return None


def repair_config(cfg_path, name, obs):
    """Fix the config defects monitor knows the right value for. Returns what it changed.

    config_is_sane is all-or-nothing: one bad key and the whole revision is demoted to
    "unrevised", which throws away every other thing the model wrote. Worse, the demotion
    does not even remove the offending key -- both fallbacks deliberately start from
    `dict(existing)` so a revision's invented knobs survive an unrevised generation, so a
    config rejected for its basis gate is rebuilt with that same basis gate still in it.

    That is not hypothetical. On 2026-08-05 seed_1a46f019c609 was rejected with
    "basis_min_bp 20.0 is above the p95 of the recorded tradeable_bp (5.11); it would pass
    0.1% of 860 readings, so this strategy would effectively never buy" -- and was then
    started anyway, with basis_min_bp 20 intact, because seed_config only rebuilds the
    price band. The gate diagnosed the strategy correctly and changed nothing about it.

    Repairs are limited to values monitor can derive itself, and each is independent of
    everything else in the file:

      * `basis_min_bp` that is not a number, or is above the p95 the gate rejects. Clamped
        to BASIS_INJECT_PERCENTILE of the same recorded series -- exactly the value
        _inject_basis_gate would have chosen for a spawn -- so the arm keeps existing
        instead of being deleted. Dropped outright only when it is not a number at all.
      * The XLM band, when it is missing, non-numeric, non-positive, inverted, or anchored
        so far from the fetched price that it can never fire. Rebuilt from
        _seed_band_half_bp around the real price, which is the same band the seed fallback
        would have produced, while every other key in the file stays put.

    It deliberately does NOT repair `name`. normalize_config's docstring makes that
    argument and it still holds: a config naming another strategy means the model copied
    someone else's file wholesale, most likely the parent's, which the refine prompt hands
    it verbatim -- and then everything else in it is suspect too. That is a revision to
    reject, not to patch, and patching it here would make the name check dead code.

    Runs on every clone, revised or not, for the same reason sanitize_config does:
    config.json is committed, so `git clone` carries a bad knob down the entire lineage
    and tweak_config copies it forward verbatim.
    """
    price = obs.price if obs is not None else None
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return []           # unparseable; the existing path rebuilds it from scratch
    if not isinstance(cfg, dict):
        return []

    repairs = []

    if 'basis_min_bp' in cfg and cfg['basis_min_bp'] is not None:
        threshold = None
        try:
            threshold = float(cfg['basis_min_bp'])
        except (TypeError, ValueError):
            repairs.append(f'dropped non-numeric basis_min_bp ({cfg["basis_min_bp"]!r})')
            del cfg['basis_min_bp']
        if threshold is not None:
            values = _tradeable_bp_values()
            if values is not None and len(values) >= 30:
                ceiling = values[min(len(values) - 1,
                                     int(len(values) * BASIS_GATE_MAX_PERCENTILE))]
                if threshold > ceiling:
                    repaired = round(values[int(len(values) * BASIS_INJECT_PERCENTILE)], 2)
                    cfg['basis_min_bp'] = repaired
                    repairs.append(
                        f'basis_min_bp {threshold} -> {repaired} (was above the p'
                        f'{int(BASIS_GATE_MAX_PERCENTILE * 100)} of {len(values)} recorded '
                        f'readings, {ceiling}; clamped to the p'
                        f'{int(BASIS_INJECT_PERCENTILE * 100)} monitor seeds this arm at)')

    # Only with a real price to anchor to. Without one there is nothing to rebuild the
    # band from, and _thresholds_are_sane skips its own anchor check for the same reason.
    if price and not _thresholds_are_sane(cfg, price):
        half_bp = _seed_band_half_bp(price)
        old = (cfg.get('buy_below'), cfg.get('sell_above'))
        cfg['buy_below'] = round(price * (1 - half_bp / 10000.0), 6)
        cfg['sell_above'] = round(price * (1 + half_bp / 10000.0), 6)
        repairs.append(f'band {old[0]}/{old[1]} -> {cfg["buy_below"]}/{cfg["sell_above"]} '
                       f'(+/-{half_bp} bp around {price}; the old one could not fire)')

    if not repairs:
        return []
    try:
        json.dump(cfg, open(cfg_path, 'w'), indent=2)
    except Exception as e:
        print(f'  could not write repaired config for {name}: {e}')
        return []
    for line in repairs:
        print(f'  repaired config for {name}: {line}')
    return repairs


def _inject_basis_gate(cfg_path):
    """Give a template spawn a coin flip at trading on the DEX/CEX basis.

    Returns True if config.json was written. The population cannot invent this on its own,
    since tweak_config only scales existing thresholds and seed_config only rebuilds the
    price band, so without a mechanical channel `basis_min_bp` would enter the population
    only if the revision model happened to write it. It is now the only such channel --
    the discovered-asset injector that used to sit beside it, and share this reasoning,
    went with the rest of the extra-asset stack. A coin flip rather than always, because
    the un-gated spawns ARE the control arm -- basis_report.py's whole output is the
    comparison between the two, and seeding every spawn would leave nothing to compare
    against.

    The threshold is derived from the recorded distribution rather than being a literal,
    for a reason worth stating plainly: tradeable_bp is negative nearly all the time (the
    basis is typically a fraction of the spread -- see basis.py's calibration note), so a
    positive literal would produce a strategy that never buys, holds cash forever, scores
    a flat 1000.00, and is indistinguishable on the leaderboard from a broken one. The
    25th percentile of the recent distribution instead means "skip the buy when the venue
    is unusually bad", which is a gate that actually fires and can therefore be wrong --
    the only kind worth an experimental arm.

    Self-sequencing: refuses until there are BASIS_MIN_RECORDED_HOURS of history to draw
    that percentile from. No arm is created before there is data to feed it, so this
    needs no operator timing and no second deploy.
    """
    try:
        cfg = json.load(open(cfg_path))
    except Exception:
        return False
    if cfg.get('basis_min_bp') is not None:
        return False    # already gated (inherited or model-written); leave it alone

    if random.random() >= BASIS_INJECT_CHANCE:
        return False

    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import market_recorder
        span = market_recorder.span()
    except Exception as e:
        print(f'  no basis gate: recorded history unavailable ({e})')
        return False
    if span['hours'] < BASIS_MIN_RECORDED_HOURS:
        print(f"  no basis gate: only {span['hours']}h of recorded history "
              f"(need {BASIS_MIN_RECORDED_HOURS}h)")
        return False
    values = _tradeable_bp_values()
    if values is None:
        print('  no basis gate: recorded history unavailable')
        return False
    if len(values) < 30:
        print(f'  no basis gate: only {len(values)} tradeable_bp readings')
        return False

    threshold = round(values[int(len(values) * BASIS_INJECT_PERCENTILE)], 2)
    cfg['basis_min_bp'] = threshold
    try:
        json.dump(cfg, open(cfg_path, 'w'), indent=2)
    except Exception as e:
        print(f'  could not write basis gate ({e})')
        return False
    print(f'  basis gate: basis_min_bp={threshold} '
          f'(p{int(BASIS_INJECT_PERCENTILE * 100)} of {len(values)} readings)')
    return True


def inject_experiments(cfg_path, obs):
    """The mechanical channel that introduces novelty a config could not invent. -> bool

    One of them now. This used to run two -- discovered extra assets and the basis gate --
    and the asset half was removed with the rest of the extra-asset stack, so this is a
    thin wrapper over _inject_basis_gate kept because DOMAIN.inject_experiments is a
    contract member and because a second channel may well come back.

    Only ever called for template-derived spawns. Clones inherit their parent's basis gate
    or absence of one, which is what makes a lineage stay in the arm it was born into --
    and that is the whole basis of basis_report.py's gated-vs-control read.
    """
    return _inject_basis_gate(cfg_path)


def _required_keys_are_sane(cfg, name=None):
    """The keys every config must carry, beyond the thresholds.

    These went unchecked for a long time because they look like bookkeeping. `name` is
    not: trade_logger writes to /opt/trades/<config name>.log, and that log is what
    score.py and qualifies_for_live() read. A clone whose config says someone else's
    name logs its trades under that name -- so it looks like it never trades, while
    inflating the trade count of a strategy that might be promoted to real money on it.
    That is the specific failure `name` is passed in to catch, and it is a plausible one:
    the refine prompt hands the model the parent's entire config.json, so copying it
    wholesale (name included) is one lazy write_file away.

    schema_version is accepted as any positive int rather than pinned to 2. Nothing in
    the system actually reads config's schema_version -- portfolio.SCHEMA_VERSION is
    state.json's, a different thing -- so pinning it would reject a config over a fact
    no consumer has an opinion about.

    Missing keys are normally filled by normalize_config before this runs; what is left
    for this to catch is present-but-wrong, which monitor cannot safely guess a fix for.
    """
    cfg_name = cfg.get('name')
    if not isinstance(cfg_name, str) or not cfg_name.strip():
        return False
    if name is not None and cfg_name != name:
        return False
    try:
        if int(cfg['schema_version']) <= 0:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    try:
        if float(cfg['trade_amount_usd']) <= 0:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _thresholds_are_sane(cfg, price):
    # Guards against a revision that "succeeds" (subprocess returncode 0) but
    # leaves the clone dead-on-arrival: unset/inverted thresholds never
    # trade, and thresholds set implausibly far from the real fetched price
    # (e.g. a stale training-data price assumption slipping through despite
    # being told the ground truth) will functionally never trade either.
    try:
        buy_below = float(cfg['buy_below'])
        sell_above = float(cfg['sell_above'])
    except (KeyError, TypeError, ValueError):
        return False
    if buy_below <= 0 or sell_above <= 0:
        return False
    if buy_below >= sell_above:
        return False
    if price and (buy_below < price * 0.5 or sell_above > price * 1.5):
        return False
    return True


def _basis_gate_is_sane(cfg):
    """Reject a `basis_min_bp` the recorded basis essentially never reaches.

    `basis_ok()` blocks a buy whenever `tradeable_bp < basis_min_bp`, so the threshold is
    only a filter if the distribution actually crosses it. _inject_basis_gate derives its
    own from the 25th percentile for exactly this reason, and its docstring spells out the
    failure -- "a positive literal would produce a strategy that never buys, holds cash
    forever, scores a flat 1000.00, and is indistinguishable on the leaderboard from a
    broken one".

    On 2026-08-05 the revision model wrote one anyway: seed_c67e7ebdf3ec got
    `basis_min_bp: 20`, described in its own summary as "a modest 20 bp to be
    conservative". Measured over the 704 recorded readings at that moment, tradeable_bp
    had a median of -1.41, a 95th percentile of 5.02, and cleared 20 bp on **one** of
    them -- 0.14% of the time. The strategy could not buy, and it went straight to the top
    of the leaderboard on its untouched $1000.

    The ceiling is the 95th percentile: a veto that passes less than one reading in twenty
    is not a filter on a strategy, it is a strategy that does not trade. Fails OPEN on
    every uncertainty (no knob, no history, unreadable recorder) -- the arm has to be able
    to exist before there is data to size it against.
    """
    threshold = cfg.get('basis_min_bp')
    if threshold is None:
        return True
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        print(f'  basis_min_bp is not a number ({threshold!r})')
        return False
    values = _tradeable_bp_values()
    if values is None or len(values) < 30:
        return True
    ceiling = values[min(len(values) - 1, int(len(values) * BASIS_GATE_MAX_PERCENTILE))]
    if threshold > ceiling:
        share = sum(1 for v in values if v >= threshold) / len(values)
        print(f'  basis_min_bp {threshold} is above the p'
              f'{int(BASIS_GATE_MAX_PERCENTILE * 100)} of the recorded tradeable_bp '
              f'({ceiling}); it would pass {share * 100:.1f}% of {len(values)} readings, '
              f'so this strategy would effectively never buy')
        return False
    return True


def config_is_sane(cfg, name, obs):
    # Split apart so the smoke-test prep in prepare_smoke_config can ask the narrower
    # question it actually means (are the thresholds usable?) without a missing
    # schema_version causing it to overwrite thresholds the model deliberately chose.
    # Three checks, not four: _assets_are_sane went with the extra-asset stack, and an
    # `assets` key surviving on an inherited config is now sanitize_config's job to
    # delete rather than a reason to reject an entire revision.
    price = obs.price if obs is not None else None
    return (_required_keys_are_sane(cfg, name)
            and _thresholds_are_sane(cfg, price)
            and _basis_gate_is_sane(cfg))


# --------------------------------------------------------------------------------------
# Criterion 6, continued: the domain's half of the smoke-test gate.
# --------------------------------------------------------------------------------------

# Whether a revised main.py has to stay *importable by the backtester*, i.e. whether
# backtest.py can load a top-level decide() out of it. If it can't, backtest_strategy
# silently replays config.json's plain buy_below/sell_above rule and reports
# decide_source: 'config-thresholds' -- so beats_buy_hold, return_pct and win_rate are
# confident-looking numbers about something other than the code being ranked, and the
# revision prompt tells the model to trust them. On 2026-08-03, 122 of 130 strategies and
# 6 of the 11 running ones (including the live one) were blind in exactly this way.
#   'non-regression' (default) reject only if the candidate is blind AND the main.py it
#                    would be reverted to was not: a lineage may never go sighted->blind.
#   'strict'         reject any blind main.py. Note revert_main_py restores the parent's
#                    file, which in most lineages is itself blind, so strict mostly
#                    discards near-misses (a real decide() plus one stray top-level
#                    statement) in exchange for an equally blind parent. Worth revisiting
#                    once most of the population is sighted.
#   'off'            report the finding, gate nothing.
MAIN_PY_IMPORTABILITY = os.environ.get('MAIN_PY_IMPORTABILITY', 'non-regression')


def check_replayable(source, baseline_source, name):
    """Would the backtester still be able to see this main.py? (ok, reason), or None.

    None means "no verdict, carry on" -- and that includes every internal failure, never
    (False, ...). See importability(): this guards a fitness signal, and failing closed
    on a tooling outage would revert every revision in the population at once.

    `baseline_source` is the main.py this candidate would be reverted to. Consulted only
    to refuse a revision that would take a lineage from backtest-visible to
    backtest-blind; when both are blind the revision is kept, because reverting would
    throw away whatever else it did for no gain in visibility.
    """
    if MAIN_PY_IMPORTABILITY == 'off':
        return None
    verdict = importability(source)
    if verdict is None or verdict[0]:
        return None
    why = verdict[1]
    baseline_ok = None
    if baseline_source is not None:
        baseline_verdict = importability(baseline_source)
        baseline_ok = baseline_verdict[0] if baseline_verdict else None
    if MAIN_PY_IMPORTABILITY == 'strict' or baseline_ok:
        return False, (
            f'backtest cannot import decide() from main.py ({why}), so '
            f'beats_buy_hold would describe config.json thresholds rather than '
            f'this code' + (' -- and the main.py it replaces was importable'
                            if baseline_ok else ''))
    # Both blind: rejecting would revert to an equally blind parent, throwing away
    # whatever else the revision did to main.py for no gain in visibility. Recorded so
    # the emperor pass can see it; not gated. See MAIN_PY_IMPORTABILITY.
    print(f'NOTE: {name} is backtest-blind ({why}); the main.py it replaces was '
          f'too, so this is not treated as a regression')
    return None


def prepare_smoke_config(cfg, obs):
    """Make `cfg` runnable for a 120s smoke run without masking a real config defect.

    Thresholds are validated separately; don't let a bad config mask a main.py that would
    run fine once the fallback fixes them.
    """
    price = obs.price if obs is not None else None
    if price and not _thresholds_are_sane(cfg, price):
        cfg['buy_below'] = round(price * 0.98, 6)
        cfg['sell_above'] = round(price * 1.02, 6)
    return cfg


def check_smoke_state(raw, cfg, obs):
    """Is the state.json the smoke run wrote a legal one? (ok, detail).

    On success `detail` is appended to the harness's "ran Ns, ..." line, so it must read
    as a clause and not a sentence.

    Negative balances and a non-positive net worth are the obvious failures. The third
    one is not: a position in anything the config does not declare. Since a config can no
    longer declare anything but the XLM base leg, that check now reads "the strategy
    opened a non-XLM position", which is precisely what a revision reaching around
    execute_trade to reintroduce multi-asset trading would look like. Worth keeping for
    that reason -- it is the gate that notices the removal being undone.
    """
    price = obs.price if obs is not None else None
    marks = obs.marks if obs is not None else None
    tools = _tools()
    if tools is None:
        usd = float(raw.get('balance_usd', 0.0))
        xlm = float(raw.get('balance_xlm', 0.0))
        if usd < 0 or xlm < 0:
            return False, f'main.py wrote negative balances (usd={usd}, xlm={xlm})'
        if usd + xlm * price <= 0:
            return False, f'main.py wrote a zero/negative net worth (usd={usd}, xlm={xlm})'
        return True, f'net worth {usd + xlm * price:.2f}'

    portfolio = tools
    st = portfolio.normalize_state(raw)
    usd = st['balance_usd']
    if usd < 0:
        return False, f'main.py wrote a negative USD balance ({usd})'
    for spec, position in st['positions'].items():
        if float(position.get('amount') or 0) < 0:
            return False, f'main.py wrote a negative {spec} balance'

    declared = portfolio.declared_specs(cfg)
    undeclared = set(st['positions']) - declared
    undeclared = {s for s in undeclared
                  if float(st['positions'][s].get('amount') or 0) > 0}
    if undeclared:
        return False, (f'main.py holds non-XLM position(s): '
                       f'{", ".join(sorted(undeclared))}; this domain trades XLM only')

    marks_for_check = dict(marks or {})
    marks_for_check.setdefault('XLM', price)
    net_worth, unpriced = portfolio.net_worth(st, marks_for_check)
    if net_worth <= 0:
        return False, f'main.py wrote a zero/negative net worth ({net_worth})'
    note = f', {len(unpriced)} leg(s) unpriced' if unpriced else ''
    return True, f'net worth {net_worth:.2f}{note}'


def cleanup_scratch(scratch_name):
    """Delete what a smoke run under `scratch_name` left outside its temp directory.

    trade_logger appends to /opt/trades/<config name>.log, and those counts gate
    real-money promotion, so the scratch log has to go. The path is the domain's
    knowledge, which is why the harness cannot do this itself.
    """
    (TRADES_DIR / f'{scratch_name}.log').unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# Criterion 6, continued: building a genome from nothing, and mutating one.
# --------------------------------------------------------------------------------------

def _seed_band_half_bp(price):
    """Half-width in bp for a seeded band, chosen from candles that actually moved.

    This used to be a flat +/-2%, and that number is where a large share of the
    population's sterility came from. Replayed against the 30 days to 2026-08-05, a band
    2% either side of spot fired **zero** times: XLM's whole range over that month was
    0.1654-0.2037 and it ended at 0.1666, so "2% below spot" was a price the market never
    reached. Every seeded fallback config was therefore born unable to trade, and since
    the fallback is what every unrevised template spawn gets, that was the majority of
    them.

    regime.suggest_bands replays a grid of widths over the real candles and reports how
    many round trips each completed and what each cleared after friction. Pick at random
    among the widths that did more than one round trip at a positive net edge -- at
    random, not the best, on purpose: the top row of an in-sample fit is the most overfit
    point in it, and these seeds are also the control arm the revised batches are compared
    against, so they need to stay spread out rather than converging on one band.

    Falls back to the old constant if /opt/tools cannot be consulted or nothing in the
    grid qualifies. A quiet degradation to the previous behaviour is right here: this is a
    fallback path, and it must never raise or block a cycle.
    """
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import regime
        grid = regime.suggest_bands(price=price)
        cost = grid.get('friction_round_trip_bp') or 0.0
        # Two round trips minimum, and a net edge per round trip at least as large as the
        # toll again -- i.e. the band is at least twice the round-trip cost gross. Barely
        # clearing the spread is the trap the revision prompt already warns about ("a
        # threshold band narrower than the round-trip cost CANNOT be profitable"), and a
        # fallback config has no model behind it to notice it got the margin wrong.
        usable = [b for b in grid.get('bands', [])
                  if b.get('round_trips', 0) >= 2
                  and (b.get('net_bp_per_round_trip') or 0) >= cost]
        if usable:
            chosen = random.choice(usable)
            print(f'  seeded band +/-{chosen["half_width_bp"]} bp '
                  f'({chosen["round_trips"]} round trips over the last '
                  f'{chosen["hours"]:.0f}h, {chosen["net_bp_per_round_trip"]} bp net each)')
            return float(chosen['half_width_bp'])
        print('  no band in the grid completed 2+ profitable round trips; '
              f'seeding +/-{SEED_BAND_FALLBACK_HALF_BP:.0f} bp')
    except Exception as e:
        print(f'  band sizing unavailable ({e}); seeding '
              f'+/-{SEED_BAND_FALLBACK_HALF_BP:.0f} bp')
    return SEED_BAND_FALLBACK_HALF_BP


def seed_config(cfg_path, name, obs):
    # Used only for bootstrapping: the template's config.json ships with
    # buy_below=sell_above=0.0, which never trades and can't be nudged by
    # tweak_config (0.0 * anything is still 0.0). Seed a real range
    # around the current price instead -- see _seed_band_half_bp for how wide.
    price = obs.price if obs is not None else None
    existing = {}
    if cfg_path.exists():
        try:
            loaded = json.load(open(cfg_path))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            pass
    trade_amount_usd = existing.get('trade_amount_usd', 10.0)
    # Start from whatever is already in the file rather than a fresh literal -- see the
    # same change in tweak_config for why. A template spawn reaching this fallback
    # has usually had a revision fail a gate, and any config knob that revision invented
    # is still in the file; there is no reason for the threshold repair to take it out.
    half_bp = _seed_band_half_bp(price)
    new_cfg_data = dict(existing)
    new_cfg_data.update({
        'name': name,
        'schema_version': existing.get('schema_version', 2),
        'buy_below': round(price * (1 - half_bp / 10000.0), 6),
        'sell_above': round(price * (1 + half_bp / 10000.0), 6),
        'trade_amount_usd': trade_amount_usd,
    })
    # The dict copy above carries every key the file already had, which for an old
    # lineage can include the retired `assets` list. sanitize_config removes it too, but
    # this path writes the file afterwards in some orderings, so drop it here as well
    # rather than depending on which of the two ran last.
    new_cfg_data.pop('assets', None)
    json.dump(new_cfg_data, open(cfg_path, 'w'), indent=2)


def tweak_config(parent_cfg_path, new_cfg_path, new_name):
    # Fallback used when the master agent is unavailable or fails: copy the parent's
    # config with thresholds nudged by a small random factor (e.g. 0.95 - 1.05).
    #
    # Returns True if it wrote a config, False if the parent's is unusable -- unreadable,
    # not an object, or without a pair of numeric thresholds to scale. This is the path
    # every failed revision lands on, so it must not raise: the parent's config.json is
    # only ever as good as whatever that lineage last committed, and a bare load here
    # turned one bad ancestor into a dead monitor process for the whole population.
    try:
        parent = json.load(open(parent_cfg_path))
    except Exception as e:
        print(f'  could not read parent config {parent_cfg_path}: {e}')
        return False
    if not isinstance(parent, dict):
        print(f'  parent config {parent_cfg_path} is a {type(parent).__name__}, not an object')
        return False
    tweak = random.uniform(0.95, 1.05)
    try:
        buy_below = round(float(parent['buy_below']) * tweak, 6)
        sell_above = round(float(parent['sell_above']) * tweak, 6)
    except (KeyError, TypeError, ValueError) as e:
        print(f'  parent config {parent_cfg_path} has no usable thresholds: {e}')
        return False
    # Start from the parent's whole config rather than a fresh literal. This used to
    # rebuild the file from a fixed key set, so every key outside it was silently
    # deleted the moment a clone fell through to this fallback -- the template's own
    # news_veto_below, and any knob a revision had invented (rsi_period, stop_loss_pct,
    # take_profit_pct and friends are all live in the population right now). The
    # stripped config was then committed and inherited by the whole lineage, so a
    # parameter the explore role discovered could not survive a single unrevised
    # generation. Discovering new config parameters is the point of that role; erasing
    # them here made it unreachable. Note this can leave a knob behind whose main.py was
    # reverted -- inert data the next revision can see, which costs nothing.
    new_cfg_data = dict(parent)
    new_cfg_data.update({
        'name': new_name,
        'schema_version': parent.get('schema_version', 2),
        'buy_below': buy_below,
        'sell_above': sell_above,
        'trade_amount_usd': parent.get('trade_amount_usd', 10.0)
    })

    # The `dict(parent)` above is what makes this the migration point for the retired
    # `assets` key: a parent config written before the extra-asset stack was removed still
    # carries one, and copying the parent wholesale is exactly how it would otherwise ride
    # down the lineage forever. This used to carry the legs across with a per-leg random
    # nudge; now it drops them.
    new_cfg_data.pop('assets', None)

    json.dump(new_cfg_data, open(new_cfg_path, 'w'), indent=2)
    return True


# --------------------------------------------------------------------------------------
# Group 7: facts the revision prompt must not restate.
# --------------------------------------------------------------------------------------

def prompt_facts():
    """Every number about this domain that the revision prompt states. Never literals.

    The prompt claimed the score haircut was 0.999 for weeks while score.py enforced
    0.899, so the model was optimizing an objective it was not ranked on -- and a cost the
    prompt states while the code charges something else is the same bug. So each value
    here is read live out of the module that enforces it: score.py for the haircut,
    friction.py for what a round trip costs, stellar_trader.py for the caps that decide
    how much of a configured trade size can actually reach the network.

    Each source is wrapped separately, with the same fallbacks the prompt used to carry
    inline, so a /opt/tools outage degrades the prompt rather than killing the revision
    process at import time.
    """
    facts = {'unrealized_haircut': _score.UNREALIZED_HAIRCUT}
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import friction
        facts['xlm_round_trip_bp'] = friction.round_trip_bp('XLM')
    except Exception:
        facts['xlm_round_trip_bp'] = 12.0
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        import stellar_trader
        facts['max_trade_usd'] = stellar_trader.MAX_TRADE_USD
    except Exception:
        facts['max_trade_usd'] = 4.0
    return facts

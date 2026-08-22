#!/usr/bin/env python3
"""The Stellar DEX MARKET-MAKER domain: quote a two-sided book, earn the spread.

Selected with DOMAIN=sdex_maker. Read master_agent/domain.py first -- it is the contract
this implements, and MAKER.md is the argument for why this is a separate domain rather
than a strategy class inside domain_sdex.

WHY THIS IS NOT domain_sdex.py WITH A FLAG. Four reasons, in MAKER.md §"Why not extend
domain_sdex.py"; the load-bearing one is that the maker/taker discriminator would have to
live in config.json, and domain.py:144-146 forbids a per-strategy domain tag there
precisely because the revision LLM rewrites that file and would eventually reassign its
own domain. The rest follow: one leaderboard culling hourly at KEEP_TOP_N would cull
makers before many small spread captures accumulate, `beats_null` diverges (buy-and-hold
is not a maker's null), and `decide(price, history, state, config) -> (side, action, usd)`
is a taker's question. A maker answers a different one, and its template asks it:

    quote(book, state, config) -> {'bid': (price, usd), 'ask': (price, usd)}

Started from domain_forecast.py rather than domain_sdex.py, on MAKER.md §3's advice:
forecast is the worked example of a domain written TO the contract, while copying sdex
drags in threshold-band furniture that has to be deleted anyway. The money-boundary
members below (caps, can_execute_live, prepare_live, retire_live) do follow sdex's shape,
because they guard the same account.

WHAT THE MEASUREMENT SAYS, and why several defaults here look narrow (MAKER_PHASE1.md).
Over 8 days of recorded tape: this book's real depth sits a few bp behind a touch made of
dust, so a quote inside the touch fills and a quote outside it queues behind thousands of
dollars and does not; anchoring to the touch with half_width_bp as a FLOOR beat anchoring
at a fixed distance from the mid by 2.7x in net edge; adverse selection ate 82-92% of gross
spread capture at every width; and the whole edge decays to zero if a quote takes more than
~10s to reach the book. The seeds and the gates below are sized against that, not a guess.
"""
import json
import math
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import domain

NAME = 'sdex_maker'

# Overridable for the same reason forecast's and null's are: this domain is exercised from
# a scratch directory by selftest_domain.py and from a host with no /opt at all.
TEMPLATE_REPO = os.environ.get('MAKER_TEMPLATE_REPO', 'file:///opt/template_repo_maker')

# Matches maker_backtest.START_USD and the maker template's opening balance_usd, so
# monitor.MIN_LIVE_SCORE (1000.0, hardcoded in the loop) keeps meaning "up on where it
# started".
STARTING_SCORE = 1000.0

# A hard requirement, not a convenience. quote_executor.sync_quotes and every primitive in
# stellar_trader check PAPER_ONLY *inside* the function, so a smoke run of a candidate
# main.py cannot rest a real offer no matter what it imports.
# See domain_sdex.RANK_GRACE_S's comment: this is the value every domain shared as
# monitor.py's bare YOUNG_GRACE_S constant before it became domain-owned. A maker's score
# moves on every fill, and fills come in on the same cycle cadence as sdex's marks, so
# three cycles' worth of quoting is already enough evidence to rank on -- unchanged.
RANK_GRACE_S = 3 * 3600

SMOKE_ENV = {'PAPER_ONLY': '1'}

OBSERVE_FAILURE_NOTE = 'Could not read the XLM/USDC order book'

REPLAY_DAYS = 7
REPLAY_WINDOW = f'{REPLAY_DAYS}d of recorded order book joined to the executed trade tape'

STRATEGIES_DIR = domain.STRATEGIES_DIR
TRADES_DIR = domain.TRADES_DIR

# Background daemons this domain needs alive. TWO, not one: the book recorder AND the tape
# syncer. A maker with a book and no tape cannot tell whether anything filled, and a
# strategy that fetched its own tape would be the rate-limit incident the single-writer
# rule already exists to prevent.
_RECORDER = {'script': Path('/opt/tools/market_recorder.py'), 'args': ['--daemon'],
             'pid': TRADES_DIR / '.market_recorder.pid',
             'log': TRADES_DIR / 'market_recorder.log', 'match': 'market_recorder'}
_TAPE = {'script': Path('/opt/tools/dex_trades.py'), 'args': ['--sync-daemon'],
         'pid': TRADES_DIR / '.dex_trades.pid',
         'log': TRADES_DIR / 'dex_trades.log', 'match': 'dex_trades'}
_JOBS = (_RECORDER, _TAPE)

# Genome bounds. The upper size bound is deliberately NOT a cap -- the real cap is
# stellar_trader.MAX_RESTING_USD_PER_SIDE and it applies to live money only. This bounds
# what the PAPER book may quote, which is what the leaderboard ranks, and it is sized so a
# paper maker's fills are the same order of magnitude as the tape it trades against.
MIN_HALF_WIDTH_BP = 2.0
MAX_HALF_WIDTH_BP = 40.0
MIN_QUOTE_USD = 1.0
MAX_QUOTE_USD = 100.0
MIN_REFRESH_S = 15
MAX_REFRESH_S = 600
MIN_INVENTORY_BAND_USD = 10.0
MAX_INVENTORY_BAND_USD = 1000.0

# Fallback seed width when the recorded spread distribution cannot be read at all. Named
# rather than inline so it is obvious in a log that a seed came from the fallback and not
# from data. Set at the measured net-positive width, not at a round number.
_FALLBACK_HALF_WIDTH_BP = 5.0
_FALLBACK_QUOTE_USD = 50.0


@dataclass
class Observation:
    """One reading of the XLM/USDC book -- the touch, the near ladder, the CEX basis.

    A BOOK, not a scalar, which is the concrete reason this cannot be domain_sdex's
    Observation: a taker needs one price to compare a threshold against, a maker needs to
    know what is already resting where, because that is its queue position.
    """
    mid: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_bp: float = 0.0
    bid_depth_usd: float = 0.0
    ask_depth_usd: float = 0.0
    cex_mid: float = None
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    marks: dict = field(default_factory=dict)
    ts: float = 0.0


# --------------------------------------------------------------------------------------
# Lazy handles on /opt/tools, mirroring domain_sdex/_forecast: appended to sys.path inside
# each function that needs it, never at import, so this module stays importable (for
# domain.check(), for selftest_domain.py) on a host with no container.
# --------------------------------------------------------------------------------------

def _tool(module):
    try:
        if '/opt/tools' not in sys.path:
            sys.path.append('/opt/tools')
        return __import__(module)
    except Exception as e:
        print(f'{module} unavailable ({e})')
        return None


def _strategy_python():
    """The interpreter that actually runs strategies -- never a bare python3.

    Imported at CALL time: strat_manager mkdirs /opt/strategies as an import side effect,
    so it is unimportable outside the container.
    """
    try:
        sys.path.insert(0, '/opt')
        import strat_manager
        return strat_manager.STRATEGY_PYTHON
    except Exception:
        return sys.executable


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, value):
    try:
        with open(path, 'w') as f:
            json.dump(value, f, indent=2)
        return True
    except Exception as e:
        print(f'could not write {path} ({e})')
        return False


def _clamp(value, lo, hi, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if value != value:                       # NaN
        return default
    return max(lo, min(hi, value))


def _observed_mid():
    """The last recorded mid, or None. Never raises.

    Used to mark inventory outside a cycle, where the `obs` the loop threads around is
    not in hand -- live_track_record gets a name and nothing else. Reads the recorder
    rather than the order book on purpose: a promotion gate that makes a network call
    fails when the network does, and the recorder's last row is at most a tick old.
    """
    recorder = _tool('market_recorder')
    if recorder is None:
        return None
    try:
        tail = recorder.tail(1)
        return tail[-1].get('dex_mid') if tail else None
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# Criterion 1: fitness resolvable inside a cycle.
# --------------------------------------------------------------------------------------

def observe():
    """One reading of the book, retried, or None.

    Retried the way domain_sdex retries its price: observe() returning None costs the loop
    a whole cycle (monitor sleeps OBSERVE_FAILURE_SLEEP and continues), and a single
    Horizon timeout is not worth a cycle.
    """
    dex_price = _tool('dex_price')
    if dex_price is None:
        return None
    book = None
    for attempt in range(3):
        try:
            book = dex_price.get_orderbook('XLM')
        except Exception as e:
            print(f'order book fetch failed ({e})')
            book = None
        if book and book.get('mid'):
            break
        if attempt < 2:
            time.sleep(20)
    if not book or not book.get('mid'):
        return None

    cex_mid = None
    price_feed = _tool('price_feed')
    if price_feed is not None:
        try:
            cex_mid = price_feed.get_price()
        except Exception:
            cex_mid = None

    levels = lambda side: [{'p': lv['price'], 'usd': lv['usd']}
                           for lv in (book.get(side) or [])[:5]]
    return Observation(
        mid=book['mid'], bid=book.get('best_bid') or 0.0,
        ask=book.get('best_ask') or 0.0,
        spread_bp=round((book.get('spread_pct') or 0.0) * 10000, 2),
        bid_depth_usd=book.get('bid_depth_usd') or 0.0,
        ask_depth_usd=book.get('ask_depth_usd') or 0.0,
        cex_mid=cex_mid, bids=levels('bids'), asks=levels('asks'),
        marks={'XLM': book['mid']}, ts=time.time())


def observe_population(obs, state):
    """Nothing extra to fetch. XLM/USDC only, so the one mark is already in `obs`.

    Multi-asset making was explicitly ruled out of scope (MAKER.md "deliberately not in
    scope"), which is what collapses sdex's per-population mark fetch to nothing here. The
    member stays because the loop calls it, and because the day a second pair is quoted
    this is where its marks get gathered once instead of per strategy.
    """
    return obs


def encode_observation(obs):
    """The whole book summary as ONE argv token to master-agent.py.

    json.dumps, not sdex's bare str(price): that exists only for argv back-compat with
    hand-typed commands, and a maker's observation is not a scalar.
    """
    try:
        return json.dumps({'mid': obs.mid, 'bid': obs.bid, 'ask': obs.ask,
                           'spread_bp': obs.spread_bp, 'cex_mid': obs.cex_mid,
                           'bid_depth_usd': obs.bid_depth_usd,
                           'ask_depth_usd': obs.ask_depth_usd,
                           'bids': obs.bids, 'asks': obs.asks, 'ts': obs.ts})
    except Exception:
        return ''


def decode_observation(text):
    """Inverse of encode_observation. None on anything unusable -- including '' and None,
    which is what an omitted argv token looks like and is a legitimate state (monitor
    failed to observe), not an error."""
    if not text:
        return None
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict) or not raw.get('mid'):
            return None
        return Observation(
            mid=float(raw.get('mid') or 0.0), bid=float(raw.get('bid') or 0.0),
            ask=float(raw.get('ask') or 0.0),
            spread_bp=float(raw.get('spread_bp') or 0.0),
            bid_depth_usd=float(raw.get('bid_depth_usd') or 0.0),
            ask_depth_usd=float(raw.get('ask_depth_usd') or 0.0),
            cex_mid=raw.get('cex_mid'), bids=raw.get('bids') or [],
            asks=raw.get('asks') or [], marks={'XLM': float(raw['mid'])},
            ts=float(raw.get('ts') or 0.0))
    except Exception:
        return None


def _resting_offer_value(offers, mid, haircut):
    """USD that resting offers add to net worth. Zero, under this executor -- read on.

    MAKER.md §3.3 asks for the opposite, and the reasoning there is right about the DANGER
    and wrong about this implementation. The danger is real: if placing an offer debits
    the balance, then a strategy's score drops the instant it quotes and recovers when it
    cancels, and the loop selects for makers that never quote -- the exact shape of the
    2026-08-01 bug where 45 never-traded clones held every top slot.

    But quote_executor does not RESERVE on placement. `balance_usd` and `positions` move
    when something fills and at no other time, so the XLM behind a resting ask is still
    counted in `positions['XLM']` and the USD behind a resting bid is still counted in
    `balance_usd`. Nothing has left the books, so the score does not drop when a strategy
    quotes, so there is nothing to add back -- and adding it anyway would INVERT the bug
    into a worse one: net worth would jump the moment a quote was posted, and the loop
    would select for makers that quote enormous size and never fill. A strategy could
    then climb the leaderboard without ever touching the market.

    This is also how Stellar itself accounts: a resting offer does not reduce `balance`,
    it raises `selling_liabilities` beside it (see stellar_trader._selling_liabilities).

    The function is kept, returning 0.0, because "should resting offers be added to net
    worth" is a question anyone reading MAKER.md §3.3 will ask, and the answer needs
    somewhere to live. It becomes real the day the executor starts reserving on
    placement -- at which point uncomment the arithmetic below AND change it in
    quote_executor.inventory_usd in the same commit, since the two must agree.
    """
    return 0.0


def _haircut():
    score = _tool('score')
    try:
        return float(score.UNREALIZED_HAIRCUT)
    except Exception:
        return 0.999


def score(state_dict, obs):
    """(score, unpriced_specs) from a raw state dict. Parity member; score_path is
    authoritative and is what monitor actually calls."""
    score_mod = _tool('score')
    if score_mod is None:
        return (float(state_dict.get('balance_usd') or 0.0), [])
    marks = dict((obs.marks if obs else None) or {})
    marks.setdefault('XLM', obs.mid if obs else 0.0)
    try:
        total, unpriced = score_mod.compute_score_multi(state_dict, marks)
    except Exception as e:
        print(f'compute_score_multi failed ({e})')
        return (float(state_dict.get('balance_usd') or 0.0), [])
    total += _resting_offer_value(state_dict.get('open_offers'),
                                  marks.get('XLM'), _haircut())
    return (total, unpriced)


def score_path(strategy_path, obs):
    """The authoritative fitness call. None ONLY when state.json is genuinely unreadable.

    "Quoting but not yet filled" must return STARTING_SCORE, never None: None becomes
    -inf upstream, which sorts last AND is excluded from max_score, so a whole population
    of freshly spawned makers would look like a total scoring outage.
    """
    raw = _read_json(Path(strategy_path) / 'state.json')
    if not isinstance(raw, dict):
        return None
    total, _unpriced = score(raw, obs)
    return total


def activity_log_path(name):
    """/opt/trades/<name>.log, with sdex's "a revision renamed itself" fallback.

    Kept at that exact path because live_report.py duplicates this resolution rather than
    importing it.
    """
    direct = TRADES_DIR / f'{name}.log'
    if direct.exists():
        return direct
    config = _read_json(STRATEGIES_DIR / name / 'config.json', {})
    declared = (config or {}).get('name')
    if declared and declared != name:
        alias = TRADES_DIR / f'{declared}.log'
        if alias.exists():
            return alias
    return direct


def activity(name):
    """(count, first_ts, last_ts) of FILLS -- not requotes.

    This is the one place the count's meaning is decided, and getting it wrong is a
    real-money mistake rather than a reporting one: monitor gates promotion on
    MIN_LIVE_TRADES (20) and MIN_LIVE_AGE_S (7200), both loop constants a domain cannot
    lower. A maker that logged its quote placements would clear a 20-"trade" bar in ten
    minutes having demonstrated nothing at all. quote_executor only ever writes a line
    when something fills, which is what makes this count mean what the gate assumes.
    """
    path = activity_log_path(name)
    count, first_ts, last_ts = 0, None, None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                ts = entry.get('timestamp')
                if ts is None:
                    continue
                count += 1
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
    except Exception:
        pass
    return count, first_ts, last_ts


def config_signature(state_entry):
    """What makes two makers the same maker, for parent dedupe.

    Width, size, skew and refresh -- MAKER.md §3.1. Omitting any of them makes two
    structurally different makers look identical and burns one of the two revision slots
    a cycle has. Rounded, so floating-point noise from tweak_config does not make every
    clone unique and defeat the dedupe from the other direction.
    """
    if not isinstance(state_entry, dict):
        return None
    config = _read_json(STRATEGIES_DIR / str(state_entry.get('name', '')) / 'config.json')
    if not isinstance(config, dict):
        return None
    try:
        return (round(float(config.get('half_width_bp', 0.0)), 2),
                round(float(config.get('quote_size_usd', 0.0)), 2),
                round(float(config.get('inventory_skew_bp', 0.0)), 2),
                int(float(config.get('refresh_interval_s', 0))))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------
# Criterion 2: a cheap offline replay.
# --------------------------------------------------------------------------------------

def replay(strategy_dir):
    """Wrap maker_backtest. {'trades', 'beats_null', 'null_pct', 'raw'} or None.

    `trades` MEANS FILLS. It is the only key the loop reads, and it is the gate that
    catches the specific maker failure a taker cannot have: a strategy that quotes
    diligently, all cycle, at a price the tape never crosses. Counting ticks here would
    make that strategy indistinguishable from one that works.

    None on any internal failure and never an exception: every caller of this in the loop
    fails OPEN, deliberately, because it guards a fitness signal rather than money.
    """
    mbt = _tool('maker_backtest')
    if mbt is None:
        return None
    try:
        result = mbt.replay(str(strategy_dir), days=REPLAY_DAYS)
    except Exception as e:
        print(f'maker replay failed ({e})')
        return None
    if not isinstance(result, dict) or result.get('error'):
        if isinstance(result, dict) and result.get('error'):
            print(f'maker replay: {result["error"]}')
        return None
    return {'trades': int(result.get('trades') or 0),
            'beats_null': result.get('beats_null'),
            'null_pct': result.get('null_pct'),
            'raw': result}


def importability(source_or_path):
    """(ok, reason) for whether replay can import quote() from this main.py, or None.

    maker_backtest's walk, NOT backtest.importability_report: that one hardcodes
    `node.name == 'decide'` (backtest.py:287) and would reject every correct maker with
    "no top-level decide() function". MAKER.md §3.2 expected it to be reusable unchanged;
    it is not, and the walk itself is the only part that was.

    None means "could not tell" and every caller fails open.
    """
    mbt = _tool('maker_backtest')
    if mbt is None:
        return None
    report = getattr(mbt, 'importability_report', None)
    if report is None:
        return None
    try:
        verdict = report(source_or_path)
    except Exception as e:
        print(f'importability check failed ({e})')
        return None
    if not isinstance(verdict, tuple) or len(verdict) != 2:
        return None
    return verdict


# --------------------------------------------------------------------------------------
# Criterion 4: caps enforced outside the agent's reach.
# --------------------------------------------------------------------------------------

def live_enabled():
    """May this domain keep ANY strategy live right now? (enabled, reason). FAILS CLOSED.

    Same switch, same order and same reasons as domain_sdex.live_enabled -- read that
    one; a maker rests offers instead of taking, but "the operator turned real money off"
    and "the money boundary is halted or absent" are identical questions on both sides.
    Written out rather than imported from domain_sdex so this module keeps its own
    boundary code (it deliberately does not inherit sdex's), and because a maker will
    eventually have a reason of its own here: resting offers that outlive a halt.
    """
    enabled, reason = domain.live_switch()
    if not enabled:
        return False, reason
    if os.environ.get('PAPER_ONLY'):
        return False, 'PAPER_ONLY is set on this process'
    st = _tool('stellar_trader')
    if st is None:
        return False, 'stellar_trader unavailable; the money boundary cannot be consulted'
    try:
        halted = st._HALT_PATH.exists()
    except Exception as e:
        return False, f'could not check the stellar_trader halt switch ({e})'
    if halted:
        return False, f'{st._HALT_PATH} exists (stellar_trader kill switch)'
    return True, ''


def caps():
    """The live cap block, read off stellar_trader. Never raises; None when unreadable.

    Recording surface only -- monitor has no call site for this and live_report reads
    stellar_trader directly. Both the resting caps and the spend caps are here because a
    maker is bounded by both, and by different ones: a resting offer is an exposure
    question and a fill is a spend question.
    """
    st = _tool('stellar_trader')
    if st is None:
        return None
    try:
        return {
            'max_open_offers': st.MAX_OPEN_OFFERS,
            'max_resting_usd_per_side': st.MAX_RESTING_USD_PER_SIDE,
            'max_resting_usd_total': st.MAX_RESTING_USD_TOTAL,
            'max_inventory_skew_usd': st.MAX_INVENTORY_SKEW_USD,
            'max_offer_age_s': st.MAX_OFFER_AGE_S,
            'min_quote_width_bp': st.MIN_QUOTE_WIDTH_BP,
            'max_trade_usd': st.MAX_TRADE_USD,
            'max_daily_usd': st.MAX_DAILY_USD,
            'min_trustline_reserve_xlm': st.MIN_TRUSTLINE_RESERVE_XLM,
        }
    except Exception as e:
        print(f'caps unavailable ({e})')
        return None


def can_execute_live(name):
    """(ok, reason). FAILS CLOSED -- the only member in the contract that does.

    The maker's version of sdex's execute_trade check. A main.py that never calls
    sync_quotes cannot place a real offer, and promoting one is worse than a no-op:
    monitor writes live.flag, believes real money is resting behind quotes that were never
    placed, and the next leader change runs retire_live against an account this strategy
    never touched.

    Also rejects a main.py that reaches around the executor -- calling place_offer,
    cancel_offer or submit_trade directly is exactly the failure MAKER.md's Risks section
    predicts ("the revision LLM will try to reimplement offer management inside main.py"),
    and a strategy that does it owns offers nothing else knows about.
    """
    import ast
    main_py = STRATEGIES_DIR / name / 'main.py'
    try:
        tree = ast.parse(main_py.read_text())
    except Exception as e:
        return False, f'could not parse main.py ({e})'
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called.add(func.attr if isinstance(func, ast.Attribute)
                   else getattr(func, 'id', None))
    if 'sync_quotes' not in called:
        return False, ('main.py never calls quote_executor.sync_quotes, so it cannot '
                       'place a real offer')
    forbidden = called & {'place_offer', 'cancel_offer', 'cancel_all_offers',
                          'submit_trade', 'manage_sell_offer', 'manage_buy_offer'}
    if forbidden:
        return False, (f'main.py calls {sorted(forbidden)} directly instead of going '
                       f'through quote_executor; offer lifecycle is not the strategy\'s')
    return True, 'calls sync_quotes and does not reach around it'


def live_track_record(name):
    """Has this genome shown a MAKER's edge, or is it a long position in a rally?
    (ok, reason). FAILS CLOSED.

    Optional contract member (see domain.py criterion 4): `qualifies_for_live` consults
    it when a domain defines it, and a domain that does not define it keeps the old
    trade-count/age/score bar unchanged.

    That bar cannot see the failure this catches. Score is derived from the strategy's own
    state.json, which marks inventory to market, so in a trending market the strategy that
    ranks #1 is the one holding the most XLM -- and promoting on score alone deploys real
    money into a directional bet wearing a maker's clothes. Observed 2026-08-21: the whole
    population showed gross capture of -1.01 bp against net +25.63 bp, the difference being
    a 25% XLM rally against ~$250 of pinned inventory each, while the 7-day replay had
    every ranked strategy at a NEGATIVE net edge.

    Two questions, both asked of numbers the strategy does not write:

      1. `net_edge_usd > 0` -- spread captured minus adverse selection, from replay(),
         which is exactly "the part a maker controls" (maker_backtest.py:819) and excludes
         what the mid did on its own. NOT `beats_null`: the null is a fixed-width quoter,
         not a zero, and on this pair it has been losing money, so `beats_null` is
         satisfied by losing more slowly. On 2026-08-21 the null itself replayed at
         -31.17, and the two top-ranked strategies at -11.19 and -38.18.

      2. Inventory inside the genome's own band. Past it, one side has stood down
         (template_repo_maker/main.py:53) and the strategy is not making a market at all;
         stuck_report already prints this, but printing it and promoting anyway is how a
         directional position gets the live flag. Deliberately the instantaneous reading
         rather than a persistence window: a promotion happens at a moment, and being
         wrong here costs a delayed promotion -- the next cycle promotes it if inventory
         has come back inside -- not money.

    Fails CLOSED, unlike every other caller of replay() in the loop. replay()'s docstring
    says its callers fail open because they guard a fitness signal; this one guards real
    money, and "the replay could not run" is not evidence of an edge.
    """
    # Checked before the replay is trusted, not for the caller's benefit: maker_backtest
    # falls back to its own null quoter when it cannot load a quote() (maker_backtest.py's
    # `strategy_dir or 'null'`), so a name with no main.py behind it comes back with a
    # complete, plausible result describing a strategy that does not exist.
    if not (STRATEGIES_DIR / name / 'main.py').exists():
        return False, f'{STRATEGIES_DIR / name} has no main.py'
    result = replay(STRATEGIES_DIR / name)
    if result is None:
        return False, 'replay could not run, so no maker edge has been demonstrated'
    net = (result.get('raw') or {}).get('net_edge_usd')
    try:
        net = float(net)
    except (TypeError, ValueError):
        return False, 'replay reported no net_edge_usd, so no maker edge has been measured'
    if net != net:                                        # NaN
        return False, 'replay reported a NaN net_edge_usd'
    if net <= 0:
        null_net = (result.get('raw') or {}).get('null_net_edge_usd')
        against = f' (the null is {null_net:+.2f})' if isinstance(null_net, (int, float)) else ''
        return False, (f'{REPLAY_DAYS}d replay net edge is ${net:+.2f}{against}: spread '
                       f'capture minus adverse selection is not positive')

    config = _read_json(STRATEGIES_DIR / name / 'config.json', {}) or {}
    try:
        band = float(config.get('inventory_band_usd'))
    except (TypeError, ValueError):
        return False, 'config.json has no readable inventory_band_usd to judge inventory against'
    if band <= 0:
        return False, f'inventory_band_usd is {band}, so inventory is unbounded'

    raw_state = _read_json(STRATEGIES_DIR / name / 'state.json')
    if not isinstance(raw_state, dict):
        return False, 'state.json is unreadable, so inventory cannot be checked'
    mid = _observed_mid()
    if not mid:
        return False, 'no recorded mid available, so inventory cannot be marked'
    try:
        inventory = float(raw_state.get('balance_xlm') or 0.0) * mid
    except (TypeError, ValueError):
        return False, 'state.json has no readable balance_xlm'
    if abs(inventory) > band:
        side = 'bid' if inventory > 0 else 'ask'
        return False, (f'inventory ${inventory:.0f} is past its ${band:.0f} band, so the '
                       f'{side} is stood down: a directional position, not a two-sided quote')

    return True, (f'{REPLAY_DAYS}d replay net edge ${net:+.2f}, inventory ${inventory:.0f} '
                  f'inside its ${band:.0f} band')


def promotion_sizing(name):
    """What this genome asks for, against what the caps allow. Recorded, never enforcing.

    Read back by live_report to answer "did the caps move since this strategy was
    promoted", which is the question that matters after a human edits the boundary.
    """
    config = _read_json(STRATEGIES_DIR / name / 'config.json', {}) or {}
    limits = caps() or {}
    try:
        wanted = float(config.get('quote_size_usd') or 0.0)
    except (TypeError, ValueError):
        wanted = 0.0
    allowed = limits.get('max_resting_usd_per_side')
    return {
        'quote_size_usd': wanted,
        'half_width_bp': config.get('half_width_bp'),
        'max_resting_usd_per_side': allowed,
        'max_resting_usd_total': limits.get('max_resting_usd_total'),
        'max_open_offers': limits.get('max_open_offers'),
        # >1 means the paper genome quotes bigger than real money ever will, which is
        # normal and is why phase 4 is judged on fills rather than dollars.
        'implied_ratio': round(wanted / allowed, 2) if allowed else None,
    }


def prepare_live(name):
    """Make the account ready for `name` to quote. Must never block a promotion.

    Two jobs. The trustline, as sdex does -- a maker settles in USDC like everything else.
    And the assertion MAKER.md §3.4 asks for: ZERO open offers before the incoming
    strategy is flagged live. retire_live cancelled the outgoing strategy's; anything
    still resting here belongs to nobody, and the incoming strategy's first reconcile
    would silently adopt it.
    """
    out = {}
    st = _tool('stellar_trader')
    if st is None:
        return out
    try:
        out['USDC'] = st.ensure_trustline(st._USDC_CODE, st._USDC_ISSUER)
    except Exception as e:
        out['USDC'] = {'ok': False, 'created': False, 'reason': str(e)}
    try:
        resting = st.open_offers()
        out['offers'] = {'ok': not resting, 'created': False,
                         'reason': (None if not resting else
                                    f'{len(resting)} offer(s) still resting at promotion; '
                                    f'they belong to no live strategy')}
        if resting:
            print(f'WARNING: {len(resting)} unowned offer(s) resting as {name} goes live')
    except Exception as e:
        out['offers'] = {'ok': False, 'created': False, 'reason': str(e)}
    return out


def retire_live(old_name):
    """(ok, [lines]). CANCEL EVERY OFFER FIRST, THEN wind_down. Fails closed.

    The order is the whole point and MAKER.md §3.4 is explicit about it. Reversed,
    wind_down sizes its chunks against _sellable_xlm, which nets out selling_liabilities,
    so with asks still resting it under-sells or fails outright and reports the position
    as un-liquidated. Worse: an offer left resting can fill AFTER the handover, opening a
    position on behalf of a strategy that is no longer live and that nothing is watching.

    False aborts the leader change, which is safe -- the old leader stays live and the cull
    exempts it, so this retries next cycle rather than stranding anything.
    """
    lines = []
    st = _tool('stellar_trader')
    if st is None:
        return False, ['  could not import stellar_trader; refusing to retire']
    try:
        pulled = st.cancel_all_offers()
    except Exception as e:
        return False, [f'  cancel_all_offers raised ({e}); leader change aborted']
    lines.append(f"  cancelled {pulled.get('cancelled', 0)} offer(s), "
                 f"{pulled.get('remaining', '?')} still resting")
    if not pulled.get('ok'):
        for failure in pulled.get('failures') or []:
            lines.append(f"    offer {failure.get('id')}: {failure.get('reason')}")
        return False, lines + ['  offers still resting; leader change aborted']

    try:
        result = st.wind_down()
    except Exception as e:
        return False, lines + [f'  wind_down raised ({e}); leader change aborted']
    lines.append(f"  wind_down: liquidated={result.get('liquidated')} "
                 f"remaining_xlm={result.get('remaining_xlm')} "
                 f"chunks={result.get('chunks')}")
    if result.get('reason'):
        lines.append(f"    {result['reason']}")
    return bool(result.get('liquidated')), lines


# --------------------------------------------------------------------------------------
# Criterion 6: synthesize, repair and judge a genome.
# --------------------------------------------------------------------------------------

def _spread_samples(hours=168):
    """Recorded half-spreads in bp, for seeding. [] when the recorder has nothing."""
    recorder = _tool('market_recorder')
    if recorder is None:
        return []
    try:
        values = recorder.series('half_spread_bp', hours=hours, spec='XLM')
    except Exception:
        return []
    return [v for v in values if isinstance(v, (int, float)) and 0 < v < 500]


def _seed_half_width_bp():
    """A width drawn from the recorded half-spread distribution, or the fallback.

    Seeded from data the way domain_sdex._seed_band_half_bp seeds from the recorded basis
    distribution, and drawn AT RANDOM among usable widths rather than at the best one: the
    top row of an in-sample fit is the most overfit point in it, and these seeds are the
    population's control arm. What the data supplies is the plausible RANGE, not the
    answer.

    Quoting at or just inside the median half-spread is what puts an offer inside the
    touch, which on this book is the only place anything fills at all.
    """
    samples = sorted(_spread_samples())
    if len(samples) < 30:
        return _FALLBACK_HALF_WIDTH_BP
    lo = samples[int(len(samples) * 0.25)]
    hi = samples[int(len(samples) * 0.75)]
    width = random.uniform(min(lo, hi), max(lo, hi))
    return round(_clamp(width, MIN_HALF_WIDTH_BP, MAX_HALF_WIDTH_BP,
                        _FALLBACK_HALF_WIDTH_BP), 2)


def normalize_config(config_path, name):
    """Fill keys only the loop can know. ABSENT KEYS ONLY.

    Patching a present-but-wrong `name` would make config_is_sane's name check dead code,
    and that check is what stops a copied name writing fills into somebody else's activity
    log and inflating their promotion count.
    """
    config = _read_json(config_path)
    if not isinstance(config, dict):
        return False
    changed = False
    defaults = {'name': name, 'schema_version': 1,
                'inventory_band_usd': 250.0, 'inventory_skew_bp': 0.0,
                'refresh_interval_s': 30}
    for key, value in defaults.items():
        if key not in config:
            config[key] = value
            changed = True
    return _write_json(config_path, config) if changed else False


def sanitize_config(config_path, obs=None):
    """Re-verify and delete what fails. XLM/USDC only, so there is nothing to verify --
    except an `assets` key, which a revision copying sdex's genome will eventually
    invent and which this domain would silently ignore while the model kept tuning it."""
    config = _read_json(config_path)
    if not isinstance(config, dict) or 'assets' not in config:
        return []
    config.pop('assets', None)
    _write_json(config_path, config)
    return ['assets (this domain quotes XLM/USDC only)']


def repair_config(config_path, name, obs=None):
    """Fix what the domain can derive. Returns the lines it printed.

    Runs on every clone, and it exists because config_is_sane is all-or-nothing while the
    seed/tweak fallbacks start from dict(existing): without this, a knob that got a config
    rejected SURVIVES the rejection and poisons the whole lineage below it.
    """
    config = _read_json(config_path)
    if not isinstance(config, dict):
        return []
    lines = []
    original = dict(config)

    width = _clamp(config.get('half_width_bp'), MIN_HALF_WIDTH_BP, MAX_HALF_WIDTH_BP, None)
    if width is None:
        width = _seed_half_width_bp()
    if width != original.get('half_width_bp'):
        config['half_width_bp'] = round(width, 3)
        lines.append(f"  repaired half_width_bp {original.get('half_width_bp')} "
                     f"-> {config['half_width_bp']}")

    size = _clamp(config.get('quote_size_usd'), MIN_QUOTE_USD, MAX_QUOTE_USD, None)
    if size is None:
        size = _FALLBACK_QUOTE_USD
    if size != original.get('quote_size_usd'):
        config['quote_size_usd'] = round(size, 2)
        lines.append(f"  repaired quote_size_usd {original.get('quote_size_usd')} "
                     f"-> {config['quote_size_usd']}")

    refresh = _clamp(config.get('refresh_interval_s'), MIN_REFRESH_S, MAX_REFRESH_S, 30)
    if refresh != original.get('refresh_interval_s'):
        config['refresh_interval_s'] = int(refresh)
        lines.append(f"  repaired refresh_interval_s {original.get('refresh_interval_s')} "
                     f"-> {config['refresh_interval_s']}")

    band = _clamp(config.get('inventory_band_usd'), MIN_INVENTORY_BAND_USD,
                  MAX_INVENTORY_BAND_USD, 250.0)
    if band != original.get('inventory_band_usd'):
        config['inventory_band_usd'] = round(band, 2)
        lines.append(f"  repaired inventory_band_usd {original.get('inventory_band_usd')} "
                     f"-> {config['inventory_band_usd']}")

    # Deliberately NOT repaired: `name`. See normalize_config.
    if lines:
        _write_json(config_path, config)
    for line in lines:
        print(line)
    return lines


def config_is_sane(config, name, obs=None):
    """The all-or-nothing verdict on a revised genome.

    Width, size, inventory band, refresh, and the name -- MAKER.md §3.1. There is
    deliberately NO `buy_below < sell_above` check: that is domain_sdex's
    _thresholds_are_sane and it is a statement about a TAKER, whose two prices straddle a
    remembered CEX mid. A maker's two prices straddle the live DEX touch and the sanity
    question is about half-width, size and inventory instead. Similar-looking numbers with
    different meanings is exactly the trap MAKER.md's "one trap to resist" names.
    """
    if not isinstance(config, dict):
        return False
    if config.get('name') != name:
        # A copied name makes quote_executor write fills into another strategy's activity
        # log, which inflates that strategy's promotion count with trades it never made.
        print(f"  config name {config.get('name')!r} does not match {name!r}")
        return False
    try:
        width = float(config.get('half_width_bp'))
        size = float(config.get('quote_size_usd'))
        refresh = float(config.get('refresh_interval_s', 30))
        band = float(config.get('inventory_band_usd', 250.0))
    except (TypeError, ValueError):
        print('  config is missing or has non-numeric maker knobs')
        return False
    if any(v != v for v in (width, size, refresh, band)):
        return False
    if not MIN_HALF_WIDTH_BP <= width <= MAX_HALF_WIDTH_BP:
        print(f'  half_width_bp {width} outside [{MIN_HALF_WIDTH_BP}, '
              f'{MAX_HALF_WIDTH_BP}]')
        return False
    if not MIN_QUOTE_USD <= size <= MAX_QUOTE_USD:
        print(f'  quote_size_usd {size} outside [{MIN_QUOTE_USD}, {MAX_QUOTE_USD}]')
        return False
    if not MIN_REFRESH_S <= refresh <= MAX_REFRESH_S:
        print(f'  refresh_interval_s {refresh} outside [{MIN_REFRESH_S}, '
              f'{MAX_REFRESH_S}]')
        return False
    if not MIN_INVENTORY_BAND_USD <= band <= MAX_INVENTORY_BAND_USD:
        print(f'  inventory_band_usd {band} outside [{MIN_INVENTORY_BAND_USD}, '
              f'{MAX_INVENTORY_BAND_USD}]')
        return False
    return True


def inject_experiments(config_path, obs=None):
    """Mechanically introduce novelty a template spawn could not invent. Coin flip.

    A coin flip and not always, because the un-injected spawns are the control arm -- an
    experiment every arm receives measures nothing. The arm is inventory skew, which
    MAKER_PHASE1.md measured as worth more than the width: the no-skew null was net
    negative over 8 days while the same width with a 4 bp skew was net positive.
    """
    if random.random() >= 0.5:
        return False
    config = _read_json(config_path)
    if not isinstance(config, dict):
        return False
    config['inventory_skew_bp'] = round(random.uniform(1.0, 8.0), 2)
    config['inventory_band_usd'] = round(random.uniform(50.0, 400.0), 2)
    config['experiment'] = 'inventory_skew'
    if _write_json(config_path, config):
        print(f"  injected inventory skew {config['inventory_skew_bp']} bp over a "
              f"${config['inventory_band_usd']} band")
        return True
    return False


def seed_config(config_path, name, obs=None):
    """Build a genome from scratch, seeded from the recorded spread distribution.

    MUST start from dict(existing): inject_experiments wrote to this same file moments
    ago, and clearing keys here would wipe the experiment before it ever ran.
    """
    config = _read_json(config_path)
    if not isinstance(config, dict):
        config = {}
    config = dict(config)
    config['name'] = name
    config.setdefault('schema_version', 1)
    config['half_width_bp'] = _seed_half_width_bp()
    config['quote_size_usd'] = round(random.uniform(10.0, 60.0), 2)
    config.setdefault('inventory_band_usd', round(random.uniform(50.0, 400.0), 2))
    config.setdefault('inventory_skew_bp', 0.0)
    config.setdefault('refresh_interval_s', random.choice([15, 30, 60, 120]))
    _write_json(config_path, config)
    print(f"  seeded {name}: {config['half_width_bp']} bp half-width, "
          f"${config['quote_size_usd']} per side, "
          f"skew {config['inventory_skew_bp']} bp, "
          f"band ${config.get('inventory_band_usd', 250.0)}, "
          f"refresh {config['refresh_interval_s']}s")


def tweak_config(parent_path, new_path, name):
    """Mutate a parent's genome. False is a supported outcome -- monitor then seeds.

    Starts from dict(parent) rather than rebuilding from a fixed key set: domain_sdex
    records that rebuilding silently deleted every knob a revision had invented, which
    made the explore role unreachable. Never raises -- one bad ancestor used to kill the
    monitor process for the whole population.
    """
    try:
        parent = _read_json(parent_path)
        if not isinstance(parent, dict):
            return False
        config = dict(parent)
        config['name'] = name
        width = _clamp(parent.get('half_width_bp'), MIN_HALF_WIDTH_BP,
                       MAX_HALF_WIDTH_BP, None)
        size = _clamp(parent.get('quote_size_usd'), MIN_QUOTE_USD, MAX_QUOTE_USD, None)
        if width is None or size is None or width <= 0 or size <= 0:
            return False        # nothing to scale; 0.0 * anything is still 0.0
        config['half_width_bp'] = round(_clamp(width * random.uniform(0.75, 1.3),
                                               MIN_HALF_WIDTH_BP, MAX_HALF_WIDTH_BP,
                                               width), 3)
        config['quote_size_usd'] = round(_clamp(size * random.uniform(0.8, 1.25),
                                                MIN_QUOTE_USD, MAX_QUOTE_USD, size), 2)
        skew = _clamp(parent.get('inventory_skew_bp', 0.0), 0.0, 20.0, 0.0)
        config['inventory_skew_bp'] = round(_clamp(skew * random.uniform(0.7, 1.4)
                                                   if skew else random.uniform(0.0, 4.0),
                                                   0.0, 20.0, skew), 2)
        # Perturb the inventory band so evolution can explore it through cloning, not
        # only through LLM revisions. Without this, every clone inherits the parent's
        # band exactly, and since the default (250.0) and most parents share it, the
        # whole population is stuck at the same band width. Measured 2026-08-21: 5 of
        # the top 8 strategies had inventory past their $250 band, the bid stood down
        # on each, and the only path to a different band was an LLM revision -- which
        # 25% of cycles (control arm) do not produce at all.
        band = _clamp(parent.get('inventory_band_usd', 250.0),
                      MIN_INVENTORY_BAND_USD, MAX_INVENTORY_BAND_USD, 250.0)
        config['inventory_band_usd'] = round(_clamp(band * random.uniform(0.7, 1.5),
                                                    MIN_INVENTORY_BAND_USD,
                                                    MAX_INVENTORY_BAND_USD, band), 2)
        if not _write_json(new_path, config):
            return False
        print(f"  tweaked from parent: {config['half_width_bp']} bp, "
              f"${config['quote_size_usd']}, skew {config['inventory_skew_bp']} bp, "
              f"band ${config['inventory_band_usd']}")
        return True
    except Exception as e:
        print(f'  tweak_config failed ({e}); falling back to a fresh seed')
        return False


def check_replayable(source, baseline_source, name):
    """(ok, reason) or None -- a non-regression policy on importability.

    A revision may not TAKE AWAY the ability to replay: if the baseline imported quote()
    and the candidate does not, the candidate's real logic becomes invisible to its own
    fitness check while replay() keeps returning confident-looking numbers for the
    config-genome fallback. A candidate that was already unimportable is not made worse
    by staying so, and rejecting it here would be a different policy than the one that
    let its parent in.

    Never returns (False, ...) for an internal failure -- failing closed on a tooling
    outage would revert every revision in the population at once.
    """
    candidate = importability(source)
    if candidate is None:
        return None
    if candidate[0]:
        return True, candidate[1]
    if baseline_source is None:
        return None
    baseline = importability(baseline_source)
    if baseline is None or not baseline[0]:
        return None                 # was already unimportable; not a regression
    return False, (f'{name} would stop being replayable: {candidate[1]} '
                   f'(its parent was importable)')


def prepare_smoke_config(config, obs=None):
    """Make a config runnable for the 120s smoke test without hiding what it does.

    Only the inert template defaults are widened. A genuinely bad genome must still be
    allowed to run badly -- the smoke test is about whether main.py WORKS, and silently
    replacing the knobs it is meant to exercise would make it prove nothing.
    """
    config = dict(config or {})
    try:
        width = float(config.get('half_width_bp') or 0.0)
    except (TypeError, ValueError):
        width = 0.0
    try:
        size = float(config.get('quote_size_usd') or 0.0)
    except (TypeError, ValueError):
        size = 0.0
    if width < MIN_HALF_WIDTH_BP:
        config['half_width_bp'] = _FALLBACK_HALF_WIDTH_BP
    if size <= 0:
        config['quote_size_usd'] = _FALLBACK_QUOTE_USD
    return config


def check_smoke_state(raw_state, config, obs=None):
    """(ok, detail). `detail` is appended to 'ran Ns, ...' so it must read as a CLAUSE.

    Three checks. Two are sdex's -- a negative balance or a non-positive net worth means
    the candidate's accounting is broken whatever else it did. The third is the maker's,
    and MAKER.md's Risks section names it: a candidate that finishes the smoke run with
    offers still open has reimplemented offer management inside main.py, or has broken the
    stand-down path, and either way it is the failure mode that loses money while nothing
    is running.
    """
    if not isinstance(raw_state, dict):
        return False, 'state.json is not an object'
    try:
        balance = float(raw_state.get('balance_usd'))
    except (TypeError, ValueError):
        return False, 'state.json has no usable balance_usd'
    if balance < 0:
        return False, f'balance_usd went negative ({balance:.2f})'
    total, _unpriced = score(raw_state, obs)
    if total is None or total <= 0:
        return False, f'net worth is not positive ({total})'
    offers = raw_state.get('open_offers')
    if offers:
        # The message names the cause, not just the symptom. It used to read "offer
        # lifecycle belongs to quote_executor, not main.py", which is true in general and
        # wrong as a diagnosis here: the candidate had not reimplemented anything, it had
        # simply been SIGTERMed, and Python's default disposition for that kills the
        # interpreter without unwinding the stack -- so the `finally: stand_down()` every
        # one of these files inherits from the template never ran. Every revision in the
        # 2026-08-19 cycles failed on this, and the accusatory wording sent the model
        # hunting through its quoting code while the fix was one registration call.
        # The last two sentences exist because the obvious fix is itself a trap: a
        # top-level signal.signal(...) or atexit.register(...) is a bare Expr, which
        # check_replayable rejects, and two of the four retries died there.
        return False, (
            f'{len(offers)} offer(s) still open after the smoke run: the smoke test ends '
            f'by sending SIGTERM, and Python kills the process on that signal without '
            f'running `finally`, so main.py never reached quote_executor.stand_down. '
            f'Install a SIGTERM/SIGINT handler that raises (SystemExit or '
            f'KeyboardInterrupt) so the existing `finally` block runs. Register it from '
            f'INSIDE main() by calling a top-level helper function -- `signal.signal(...)` '
            f'or `atexit.register(...)` written at module top level is a bare call '
            f'expression, which is not importable and will get this revision reverted for '
            f'a different reason. See _install_stop_handlers in the template.')
    fills = int(raw_state.get('fills_total') or 0)
    quoted = int(raw_state.get('quoted_sides') or 0)
    return True, (f'net worth ${total:.2f}, {fills} fill(s), '
                  f'{quoted} side(s) quoted at exit')


def cleanup_scratch(scratch_name):
    """Delete everything a smoke run wrote under its throwaway name.

    The activity log especially: counts written by a scratch run would otherwise be read
    by activity() and gate a real-money promotion.
    """
    try:
        activity_log_path(scratch_name).unlink(missing_ok=True)
    except Exception:
        pass
    for suffix in ('.pubnet.log', '.run.log'):
        try:
            (TRADES_DIR / f'{scratch_name}{suffix}').unlink(missing_ok=True)
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# Criterion 5's instruments: is this an edge or a habit?
# --------------------------------------------------------------------------------------

def _fill_stats(name, hours=24):
    """Live edge from the activity log: (fills, volume, buys, sells, spread_usd).

    `spread_usd` is measured the same way maker_backtest measures it -- per fill, the
    distance between the reference mid recorded on the line and the price it actually
    transacted at -- so a live number and a replay number mean the same thing and can be
    compared. record_trade writes `price` as the reference mid and `fill_price` as the
    resting price, which is what makes this recoverable from the log at all.
    """
    cutoff = time.time() - hours * 3600
    fills = volume = buys = sells = 0
    spread = 0.0
    try:
        with open(activity_log_path(name)) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if (entry.get('timestamp') or 0) < cutoff:
                    continue
                fills += 1
                usd = abs(entry.get('amount_usd') or 0.0)
                volume += usd
                mid, fill = entry.get('price'), entry.get('fill_price')
                if entry.get('action') == 'maker_buy':
                    buys += 1
                    if mid and fill:
                        spread += usd * (mid - fill) / mid
                else:
                    sells += 1
                    if mid and fill:
                        spread += usd * (fill - mid) / mid
    except Exception:
        pass
    return fills, volume, buys, sells, spread


def _live_net(name, mid):
    """Net worth change against STARTING_SCORE, at `mid`. The live bottom line."""
    raw = _read_json(STRATEGIES_DIR / name / 'state.json')
    if not isinstance(raw, dict):
        return None
    try:
        return (float(raw.get('balance_usd') or 0.0)
                + float(raw.get('balance_xlm') or 0.0) * (mid or 0.0) * _haircut()
                - STARTING_SCORE)
    except Exception:
        return None


def report_activity(performances, limit=None):
    """Turnover against edge -- LIVE, with the replay's verdict alongside it.

    Every column left of `bt_net$` is measured from this strategy's own fill log and its
    own state.json; `bt_net$` is the only one that comes from replay(), and it is labelled
    because it describes an 8-day historical window rather than anything that just
    happened. An earlier version of this function printed live fill counts and REPLAY edge
    numbers in the same row with no marking, which reads as a live edge and is not one --
    the live and replayed numbers diverged by about 1.3 bp of volume the first time they
    were compared, which is the whole reason this table exists.

    Gross capture and the residual are printed SEPARATELY and never only netted: a maker
    that reports gross capture alone always looks profitable (MAKER.md 1.1). `resid$` is
    net minus captured -- what the mid did to the inventory each fill left behind, plus
    whatever the XLM price did on its own.
    """
    rows = list(performances or [])[:limit or 8]
    if not rows:
        return
    obs_mid = _observed_mid()
    print('Maker activity (24h live; bt_net$ is the 8-day replay, not live):')
    print(f"  {'strategy':<26} {'fills':>6} {'buy/sell':>10} {'volume$':>9} "
          f"{'gross$':>8} {'resid$':>8} {'net$':>8} {'bt_net$':>8}")
    total_vol = total_gross = total_net = 0.0
    for name, _score in rows:
        fills, volume, buys, sells, gross = _fill_stats(name)
        net = _live_net(name, obs_mid)
        result = replay(STRATEGIES_DIR / name)
        bt = (result or {}).get('raw', {}).get('net_edge_usd')
        total_vol += volume
        total_gross += gross
        if net is not None:
            total_net += net
        print(f"  {name:<26} {fills:>6} {f'{buys}/{sells}':>10} {volume:>9.0f} "
              f"{gross:>8.3f} {(net - gross) if net is not None else float('nan'):>8.3f} "
              f"{net if net is not None else float('nan'):>8.3f} "
              f"{bt if bt is not None else float('nan'):>8.3f}")
    if total_vol > 0:
        print(f"  {'TOTAL':<26} {'':>6} {'':>10} {total_vol:>9.0f} "
              f"{total_gross:>8.3f} {total_net - total_gross:>8.3f} {total_net:>8.3f}")
        print(f"  per unit volume: gross {1e4 * total_gross / total_vol:+.2f} bp, "
              f"net {1e4 * total_net / total_vol:+.2f} bp")


def stuck_report(performances, state, obs):
    """The maker's dead ends, or None. Two shapes, both invisible on a leaderboard.

    Quoting-but-never-filling is the one a taker cannot have: a threshold strategy that
    never fires is idle and obvious, whereas a maker at a width the tape never crosses
    looks fully occupied. Pinned inventory is the other -- one side permanently stood down
    means the strategy is a directional position wearing a maker's clothes.
    """
    lines = []
    for name, _score in list(performances or [])[:12]:
        raw_state = _read_json(STRATEGIES_DIR / name / 'state.json')
        if not isinstance(raw_state, dict):
            continue
        fills, _volume, _buys, _sells, _gross = _fill_stats(name, hours=6)
        quoted = int(raw_state.get('quoted_sides') or 0)
        if quoted > 0 and fills == 0:
            config = _read_json(STRATEGIES_DIR / name / 'config.json', {}) or {}
            lines.append(f"  {name}: quoting {quoted} side(s) at "
                         f"{config.get('half_width_bp')} bp but 0 fills in 6h -- "
                         f"the tape never crossed it")
        # The XLM leg alone -- resting offers are NOT added, for the reason
        # _resting_offer_value documents: nothing was reserved when they were placed, so
        # positions already contains them.
        mid = obs.mid if obs else 0.0
        inventory = float(raw_state.get('balance_xlm') or 0.0) * mid
        band = float((_read_json(STRATEGIES_DIR / name / 'config.json', {}) or {})
                     .get('inventory_band_usd') or 0.0)
        if band and abs(inventory) > band:
            side = 'bid' if inventory > 0 else 'ask'
            lines.append(f"  {name}: inventory ${inventory:.0f} past its ${band:.0f} "
                         f"band -- the {side} has been stood down")
    if not lines:
        return None
    return 'Maker dead ends:\n' + '\n'.join(lines)


def report_regime(obs):
    """One line: where the current spread sits in the recorded distribution, and how much
    the tape is actually trading. Both are the inputs a width decision needs."""
    if obs is None:
        return
    samples = sorted(_spread_samples(hours=168))
    half_bp = obs.spread_bp / 2.0 if obs.spread_bp else None
    percentile = None
    if samples and half_bp:
        below = sum(1 for v in samples if v <= half_bp)
        percentile = round(100.0 * below / len(samples))
    tape = _tool('dex_trades')
    volume = trades = None
    if tape is not None:
        try:
            stats = tape.tape_stats(hours=24)
            bucket = (stats.get('buckets') or {}).get('>=1') or {}
            volume, trades = bucket.get('usd_per_hour'), bucket.get('per_hour')
        except Exception:
            pass
    print(f'Book: {obs.bid:.7f} / {obs.ask:.7f}, spread {obs.spread_bp} bp'
          + (f' (half-spread at the {percentile}th percentile of 7d)'
             if percentile is not None else '')
          + f', depth ${obs.bid_depth_usd:.0f}/${obs.ask_depth_usd:.0f}'
          + (f'; tape {trades}/h, ${volume}/h at >=$1' if trades is not None else ''))


def report_experiments():
    """Gated-vs-control readout for the inventory-skew arm inject_experiments creates."""
    with_skew, without = [], []
    try:
        for path in sorted(STRATEGIES_DIR.glob('*/config.json')):
            config = _read_json(path, {}) or {}
            name = path.parent.name
            fills, volume, _b, _s, _g = _fill_stats(name, hours=24)
            bucket = with_skew if float(config.get('inventory_skew_bp') or 0.0) > 0 else without
            bucket.append((name, fills, volume))
    except Exception as e:
        print(f'experiment report unavailable ({e})')
        return
    if not with_skew and not without:
        return

    def line(label, rows):
        if not rows:
            return f'  {label}: none'
        fills = sum(r[1] for r in rows)
        volume = sum(r[2] for r in rows)
        return (f'  {label}: {len(rows)} strategies, {fills} fills, '
                f'${volume:.0f} volume (24h)')
    print('Inventory-skew experiment:')
    print(line('with skew', with_skew))
    print(line('control  ', without))


def report_live(live_name):
    """What is actually resting, what filled, and whether the two agree.

    Reconciles our recorded offers against Horizon rather than reporting our own belief:
    fill detection is polling and therefore lossy at the edges, and reconciliation against
    on-chain truth is what makes that safe rather than a faster poll.
    """
    st = _tool('stellar_trader')
    if st is not None:
        try:
            status = st.offer_status()
            print(f"Live offers: {status['open']}/{status['max_open']} open, "
                  f"${status['resting_usd_total']:.2f} resting "
                  f"(bid ${status['resting_usd']['bid']:.2f} / "
                  f"ask ${status['resting_usd']['ask']:.2f})")
            raw_state = _read_json(STRATEGIES_DIR / live_name / 'state.json', {}) or {}
            expected = {str(o['offer_id']): o for o in (raw_state.get('open_offers') or [])
                        if o.get('offer_id')}
            report = st.reconcile_offers(expected)
            if report.get('unknown'):
                print(f"  WARNING {len(report['unknown'])} resting offer(s) not in "
                      f"{live_name}'s state.json")
            if report.get('stale'):
                print(f"  WARNING {len(report['stale'])} offer(s) past "
                      f"MAX_OFFER_AGE_S")
        except Exception as e:
            print(f'offer status unavailable ({e})')
    live_report = _tool('live_report')
    if live_report is None:
        try:
            sys.path.insert(0, '/opt/master_agent')
            import live_report
        except Exception:
            return
    try:
        print(live_report.summary_line(live_name))
    except Exception as e:
        print(f'live report unavailable ({e})')


def _job_alive(job):
    """Pid file plus os.kill plus /proc/<pid>/cmdline.

    os.kill(pid, 0) alone is not enough: the pid file survives a container restart and pids
    are recycled, so a stale file can name a live and entirely unrelated process.
    """
    try:
        pid = int(Path(job['pid']).read_text().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    try:
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes().decode('utf-8', 'replace')
    except Exception:
        return False
    return job['match'] in cmdline


def background_jobs_alive():
    """True only if BOTH daemons are confirmed. A book with no tape cannot detect a fill."""
    return all(_job_alive(job) for job in _JOBS)


def ensure_background_jobs():
    """Start the book recorder and the tape syncer if either is down, then report.

    _strategy_python(), never a bare python3: /usr/bin/python3 cannot import the packages
    in /opt/agents/venv, and a bare python3 is what made every revision fail silently for
    weeks in this codebase's history. setsid so the daemons outlive a TERM to monitor's
    process group -- they must survive a cycle and an emperor window.
    """
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    for job in _JOBS:
        if _job_alive(job):
            continue
        try:
            log = open(job['log'], 'a')
            proc = subprocess.Popen(
                [_strategy_python(), '-u', str(job['script'])] + job['args'],
                stdout=log, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
            Path(job['pid']).write_text(str(proc.pid))
            print(f"Started {job['match']} (pid {proc.pid}) -> {job['log']}")
        except Exception as e:
            print(f"could not start {job['match']} ({e})")

    recorder = _tool('market_recorder')
    if recorder is not None:
        try:
            span = recorder.span()
            row = recorder.tail(1)
            last = row[-1] if row else {}
            age = round(time.time() - last['ts']) if last.get('ts') else None
            ladder = 'with ladder' if last.get('bid_cum') else 'NO LADDER'
            print(f"Market history: {span['rows']} rows over {span['hours']}h; "
                  f"last row {age}s ago, {ladder}, spread {last.get('spread_bp')} bp")
            if age is not None and age > 300:
                print(f"WARNING: market history is {age}s stale; every maker is "
                      f"quoting off a stale book. Check {_RECORDER['log']}")
        except Exception as e:
            print(f'market history unavailable ({e})')

    tape = _tool('dex_trades')
    if tape is not None:
        try:
            covered = tape.span()
            age = (round(time.time() - covered['last_ts'])
                   if covered.get('last_ts') else None)
            print(f"Trade tape: {covered['rows']} trades over {covered['days']}d; "
                  f"last trade {age}s ago")
            if covered['days'] < REPLAY_DAYS:
                print(f"WARNING: only {covered['days']}d of tape cached; replay() wants "
                      f"{REPLAY_DAYS}d. Run dex_trades.backfill(days={REPLAY_DAYS}).")
        except Exception as e:
            print(f'trade tape unavailable ({e})')


# --------------------------------------------------------------------------------------
# Group 7: facts the revision prompt must not restate.
# --------------------------------------------------------------------------------------

def prompt_facts():
    """Every number the maker prompt states, read LIVE from whatever enforces it.

    Not one of these may be written as a literal in master-agent.py's prose. The revision
    prompt told the model the score haircut was 0.999 for weeks while score.py enforced
    0.899, so the model was optimizing an objective it was not ranked on -- and MAKER.md's
    "one trap to resist" names that same incident. Each source is wrapped separately, so
    one unimportable module degrades one number instead of emptying the dict.
    """
    facts = {
        'starting_score': STARTING_SCORE,
        'min_half_width_bp': MIN_HALF_WIDTH_BP,
        'max_half_width_bp': MAX_HALF_WIDTH_BP,
        'min_quote_usd': MIN_QUOTE_USD,
        'max_quote_usd': MAX_QUOTE_USD,
        'min_refresh_s': MIN_REFRESH_S,
        'max_refresh_s': MAX_REFRESH_S,
        'replay_days': REPLAY_DAYS,
        'replay_window': REPLAY_WINDOW,
        'unrealized_haircut': _haircut(),
        'executor': 'quote_executor.sync_quotes',
        'entry_point': 'quote(book, state, config)',
    }
    st = _tool('stellar_trader')
    for key, attr in (('max_open_offers', 'MAX_OPEN_OFFERS'),
                      ('max_resting_usd_per_side', 'MAX_RESTING_USD_PER_SIDE'),
                      ('max_resting_usd_total', 'MAX_RESTING_USD_TOTAL'),
                      ('max_inventory_skew_usd', 'MAX_INVENTORY_SKEW_USD'),
                      ('max_offer_age_s', 'MAX_OFFER_AGE_S'),
                      ('min_quote_width_bp', 'MIN_QUOTE_WIDTH_BP'),
                      ('max_trade_usd', 'MAX_TRADE_USD')):
        try:
            facts[key] = getattr(st, attr)
        except Exception:
            facts[key] = None
    try:
        friction = _tool('friction')
        facts['round_trip_bp'] = round(friction.round_trip_bp('XLM'), 2)
    except Exception:
        facts['round_trip_bp'] = None
    try:
        mbt = _tool('maker_backtest')
        facts['fill_lag_s'] = mbt.FILL_LAG_S
    except Exception:
        facts['fill_lag_s'] = None
    return facts


def observation_line(obs):
    """The book as ground truth, for the revision prompt. Handles obs is None.

    Framed as "right now", the same way domain_sdex frames its price: without it the model
    anchors on whatever XLM traded at in its training data, and writes a width against a
    spread that has not existed for a year.
    """
    if obs is None:
        return 'No order book reading was available for this revision.\n'
    lines = [f'The XLM/USDC order book RIGHT NOW (ground truth, not a historical or '
             f'typical reading):',
             f'  bid {obs.bid:.7f}  ask {obs.ask:.7f}  mid {obs.mid:.7f}  '
             f'spread {obs.spread_bp} bp',
             f'  depth ${obs.bid_depth_usd:.0f} bid / ${obs.ask_depth_usd:.0f} ask']
    if obs.bids:
        near = ', '.join(f"{lv['p']:.7f}=${lv['usd']:.2f}" for lv in obs.bids[:3])
        lines.append(f'  top bids {near}')
    if obs.asks:
        near = ', '.join(f"{lv['p']:.7f}=${lv['usd']:.2f}" for lv in obs.asks[:3])
        lines.append(f'  top asks {near}')
    if obs.cex_mid:
        basis = (obs.mid - obs.cex_mid) / obs.cex_mid * 10000
        lines.append(f'  CEX mid {obs.cex_mid:.7f}, basis {basis:+.1f} bp')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    problems = domain.check(sys.modules[__name__])
    print(f'domain: {NAME}')
    for problem in problems:
        print(f'  FAIL {problem}')
    print(f'  {len(problems)} contract problem(s)')
    sys.exit(1 if problems else 0)

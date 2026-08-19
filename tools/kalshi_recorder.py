#!/usr/bin/env python3
"""A durable, shared record of Kalshi market conditions -- one writer, many readers.

Direct structural copy of market_recorder.py's pattern (see that module's docstring for
the full rationale): kalshi_api.py's reads are individually cheap but uncached, and a
population of strategies each independently polling the markets they're forecasting on
would turn into N-strategies-times-a-tick-loop worth of Kalshi requests for the exact
same data. ONE writer here (the daemon below, meant to be supervised by
domain_kalshi.ensure_background_jobs the way market_recorder.daemon is supervised by
domain_sdex.ensure_background_jobs), MANY readers (template_repo_kalshi/main.py's
decide() loop, domain_kalshi.py's observe(), kalshi_reconcile.py).

WHY THIS DAEMON EXISTS AND forecast_engine.py's DIDN'T
=========================================================
domain_forecast.py's world is a pure function of a seed -- nothing to supervise.
Kalshi's markets are real external state that changes on its own clock, so
`background_jobs_alive()` must be able to say False here, unlike domain_forecast's
`True` unconditionally. See KALSHI.md's Phase 1 section.

WHAT "the markets a strategy population is actually forecasting on" MEANS
============================================================================
Rather than a hardcoded category, this daemon reads /opt/strategy_state.json, opens
each live strategy's config.json, and collects the (category_filter, market_frequency)
pairs actually in use across the population -- the same knobs
template_repo_kalshi/main.py's config carries and DOMAIN.tweak_config/seed_config
mutate. A cold population (no strategies yet, or none with a readable config) falls back
to DEFAULT_CATEGORY/DEFAULT_FREQUENCY, the Phase 0 spike's recommended starting filter
(KALSHI.md: "Recommend Climate and Weather dailies as category_filter's Phase 1
default"). The category set actually polled is capped (MAX_CATEGORIES) so a population
that has scattered across many categories doesn't turn one poll into an unbounded
request fan-out -- the most POPULAR categories win, which is also the direction that
keeps a specializing sub-population's data fresh.

THIN-MARKET FLOOR
==================
Rows below MIN_VOLUME are not written at all. This is the concrete mechanism for
KALSHI.md's "Thin-market gaming risk": a strategy that could only look skilled by
picking wide-spread, no-volume markets never gets the chance, because this log -- the
only cheap source decide() reads -- never contains them. See kalshi_api.py's docstring
for why last_price (not a midpoint) is still the right read for the markets that DO
clear this floor.

LOG FORMAT (market-condition history, HISTORY_PATH)
=====================================================
One JSON line per (market, poll), appended to HISTORY_PATH. Fields: ts, category,
frequency, ticker, event_ticker, title, status, result, implied_prob, yes_bid, yes_ask,
volume, open_interest, close_time -- kalshi_api.py's normalized market shape plus the
poll's ts/category/frequency tags.

SCORING: submit_forecast()/cumulative_brier(), the second thing this module owns
====================================================================================
forecast_engine.py bundles question generation AND scoring into one module because both
are pure functions of the same seed. Kalshi has no such module in KALSHI.md's file list,
and the natural home for "the market's current price, looked up cheaply" is here, next
to the daemon that already caches it -- so this module plays forecast_engine.py's dual
role for domain_kalshi.py: current_markets() is the "what can I forecast right now" half,
submit_forecast()/cumulative_brier() are the "was I right" half.

WHY SCORING CANNOT HAPPEN AT FORECAST TIME, only get RECORDED then
======================================================================
KALSHI.md's design section says to score p_hat "that instant" against p_market. Taken
literally that is impossible for a genuine skill signal: any score built ONLY from
(p_hat, p_market) with no outcome is, by the definition of a proper scoring rule, optimized
by reporting p_hat = p_market exactly -- i.e. parroting the market would be
mathematically UNbeatable, not merely a good baseline. That would make evolution
converge on pure copying, the opposite of what this domain is for. The reading that
actually works, and the one implemented here: p_market is READ AND FROZEN at forecast
time (submit_forecast, below) so the later comparison is not biased by a market price
that has since moved toward the answer -- but the SCORE (brier vs frozen p_market, both
against the real outcome) can only be computed once the market resolves. That is exactly
"the market's implied probability" null FUTURE.md names for this domain, just evaluated
honestly instead of instantly.

Append-only, deliberately, per this codebase's convention for daemon-written logs (see
market_recorder.py's docstring) -- so a forecast entry is never mutated in place once
its outcome becomes known. Instead kalshi_reconcile.py appends a SEPARATE 'resolution'
row once Kalshi settles a ticker, and cumulative_brier() joins the two row types by
ticker in memory. One resolution row settles every forecast row ever logged against that
ticker, including a strategy that forecast the same market more than once across ticks.

Those repeat rows are NOT independent bets, and cumulative_brier() no longer scores them
as if they were -- it collapses them to one observation per (ticker, wall-clock hour)
first. This paragraph used to claim the opposite ("each such bet is independently judged
against the p_market it saw at the time, which is the point"); that was wrong, and it was
wrong in the direction that quietly inflates score. Every row does still record the
p_market it saw at the time, which is what makes the frozen-price comparison honest --
but a market that settles once is one outcome no matter how many times it was forecast.
See cumulative_brier()'s docstring for what the inflation cost in practice and why the
bucket is an hour rather than a whole market.
"""
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import kalshi_api

STATE_FILE = Path('/opt/strategy_state.json')
HISTORY_PATH = Path('/opt/trades/.kalshi_market_history.jsonl')
FORECAST_LOG_DIR = Path('/opt/trades')   # per-strategy <name>.log, same dir domain_null.py
                                          # and domain_forecast.py already use

RECORD_INTERVAL = 300   # 5 min -- daily-cadence markets don't need 60s granularity

# Scoring granularity: cumulative_brier() collapses a strategy's forecast rows to one
# observation per (ticker, wall-clock hour) before scoring them. Read that function's
# docstring before changing this -- it is not a display setting, it is the unit of
# evidence domain_kalshi._shrunk_edge weights, and both an hour-sized bucket and the
# mean-within-bucket are chosen for stated reasons. Absolute epoch hours, not hours since
# a strategy's first forecast, so two strategies' buckets line up with each other.
BUCKET_SECONDS = 3600

DEFAULT_CATEGORY = 'Climate and Weather'
DEFAULT_FREQUENCY = 'daily'
MAX_CATEGORIES = 3          # distinct (category, frequency) pairs polled per cycle
SERIES_PER_CATEGORY = 8     # kalshi_api.list_open_markets's max_series, per pair
LIMIT_PER_SERIES = 20

# Rows below this volume floor are not written -- see module docstring's THIN-MARKET
# FLOOR section. Small, not zero: a market with a handful of contracts traded is real
# evidence a real bettor priced it, which last_price=0.00 on a just-opened market is not.
MIN_VOLUME = 10.0

# ~150-300 rows/poll at 5 min intervals is a few hundred KB/day; this caps the file
# regardless of how the population's category mix drifts over a long run. Mirrors
# market_recorder.py's MAX_ROWS/_ROW_BYTES guard exactly.
MAX_ROWS = 300000
_ROW_BYTES = 300


def discover_target_categories():
    """[(category, frequency), ...], most-popular-first, capped at MAX_CATEGORIES.

    Falls back to [(DEFAULT_CATEGORY, DEFAULT_FREQUENCY)] when the population is empty
    or no strategy has a readable category_filter yet -- the same cold-start answer
    domain_kalshi.seed_config will hand a fresh template spawn.
    """
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        state = {}
    counts = Counter()
    for entry in (state or {}).values():
        path = entry.get('path') if isinstance(entry, dict) else None
        if not path:
            continue
        try:
            cfg = json.loads((Path(path) / 'config.json').read_text())
        except Exception:
            continue
        category = cfg.get('category_filter')
        if not category:
            continue
        frequency = cfg.get('market_frequency', DEFAULT_FREQUENCY)
        counts[(category, frequency)] += 1
    if not counts:
        return [(DEFAULT_CATEGORY, DEFAULT_FREQUENCY)]
    return [pair for pair, _ in counts.most_common(MAX_CATEGORIES)]


def snapshot():
    """[{ts, category, frequency, ...market}, ...] for the population's current target
    categories, filtered by MIN_VOLUME. No I/O to disk."""
    ts = time.time()
    rows = []
    seen = set()
    for category, frequency in discover_target_categories():
        markets = kalshi_api.list_open_markets(
            category=category, frequency=frequency,
            max_series=SERIES_PER_CATEGORY, limit_per_series=LIMIT_PER_SERIES)
        for m in markets:
            ticker = m.get('ticker')
            if not ticker or ticker in seen:
                continue
            volume = m.get('volume')
            if volume is None or volume < MIN_VOLUME:
                continue
            seen.add(ticker)
            row = dict(m)
            row.update({'ts': ts, 'category': category, 'frequency': frequency})
            rows.append(row)
    return rows


def record_snapshot():
    """Append this poll's rows. Returns the rows written (possibly [])."""
    rows = snapshot()
    if not rows:
        return rows
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open('a') as f:
            for row in rows:
                f.write(json.dumps(row) + '\n')
        _trim()
    except Exception:
        return []
    return rows


def _trim():
    """Keep the file under MAX_ROWS, same temp-file-then-replace pattern as
    market_recorder._trim so a reader mid-trim never sees a truncated file."""
    try:
        if not HISTORY_PATH.exists():
            return
        if os.path.getsize(HISTORY_PATH) < MAX_ROWS * _ROW_BYTES:
            return
        lines = HISTORY_PATH.read_text().splitlines()
        if len(lines) <= MAX_ROWS:
            return
        tmp = HISTORY_PATH.with_suffix('.tmp')
        tmp.write_text('\n'.join(lines[-MAX_ROWS:]) + '\n')
        tmp.replace(HISTORY_PATH)
    except Exception:
        pass


def tail(n=1, max_bytes=262144):
    """The last `n` rows, oldest first. Seeks from EOF rather than scanning the whole
    file -- see market_recorder.tail's docstring for why that matters once this file
    grows past a few thousand rows."""
    try:
        if not HISTORY_PATH.exists():
            return []
        size = os.path.getsize(HISTORY_PATH)
        want = max(4096, min(int(max_bytes), max(1, int(n)) * _ROW_BYTES * 4))
        start = max(0, size - want)
        with HISTORY_PATH.open('rb') as f:
            f.seek(start)
            chunk = f.read()
        lines = chunk.decode('utf-8', 'replace').splitlines()
        if start > 0 and lines:
            lines = lines[1:]   # partial first line from an arbitrary seek offset
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out[-max(1, int(n)):]
    except Exception:
        return []


def latest_by_ticker(category=None, frequency=None, lookback_rows=2000):
    """{ticker: row} for the most recent reading of every ticker seen in the last
    `lookback_rows` -- one poll cycle writes at most a few hundred rows, so this window
    comfortably covers "the current snapshot" without scanning the whole history."""
    out = {}
    for row in tail(lookback_rows):
        if category and row.get('category') != category:
            continue
        if frequency and row.get('frequency') != frequency:
            continue
        ticker = row.get('ticker')
        if not ticker:
            continue
        out[ticker] = row   # tail() is oldest-first, so later entries overwrite
    return out


def current_markets(n=1, category=None, frequency=None):
    """Up to `n` candidate markets to forecast right now, freshest first -- the Kalshi
    analog of forecast_engine.current_questions().

    Falls back to a direct (still cached/rate-limited, via kalshi_api's own guards)
    live fetch when the shared log has NOTHING for `category` yet -- discover_target_
    categories() only polls categories the currently-REGISTERED population uses, so a
    freshly seeded or mutated strategy (a smoke-test scratch dir especially, which never
    appears in /opt/strategy_state.json at all) can otherwise starve for a full
    RECORD_INTERVAL, which check_smoke_state's SMOKE_TEST_SECONDS window would not
    survive. This does not defeat the recorder's single-writer rate-limiting purpose --
    it only fires on a genuine cache miss for that category, not on every tick. []
    only if that fallback also finds nothing (a real, reportable dead end), never
    raises."""
    latest = latest_by_ticker(category=category, frequency=frequency)
    rows = sorted(latest.values(), key=lambda r: r.get('ts', 0), reverse=True)
    if rows or not category:
        return rows[:max(1, int(n))]
    live = kalshi_api.list_open_markets(category=category, frequency=frequency,
                                        max_series=SERIES_PER_CATEGORY,
                                        limit_per_series=LIMIT_PER_SERIES)
    ts = time.time()
    live = [dict(m, ts=ts, category=category, frequency=frequency)
            for m in live if m.get('volume') is not None and m['volume'] >= MIN_VOLUME]
    live.sort(key=lambda r: r.get('volume') or 0, reverse=True)
    return live[:max(1, int(n))]


# --------------------------------------------------------------------------------------
# Scoring: submit_forecast()/cumulative_brier(). See module docstring's SCORING section
# for why these can't be forecast_engine.submit_forecast's one-shot version.
# --------------------------------------------------------------------------------------

def submit_forecast(name, ticker, p_hat):
    """Log `name`'s forecast against `ticker`'s CURRENT market price, frozen now.

    Returns the logged entry, or None for a malformed p_hat or a ticker whose price
    can't currently be read (main.py's tick loop must never die over a bad value or a
    transient Kalshi outage -- same non-raising contract as forecast_engine's version).
    Unresolved (`brier`/`market_brier` are None) until kalshi_reconcile.py finds this
    ticker has settled and appends a matching 'resolution' row -- see cumulative_brier().
    """
    try:
        p_hat = float(p_hat)
    except (TypeError, ValueError):
        return None
    if p_hat != p_hat:   # NaN
        return None
    p_hat = min(1.0, max(0.0, p_hat))
    market = kalshi_api.get_market(ticker)
    if market is None or market.get('implied_prob') is None:
        return None
    entry = {
        'type': 'forecast',
        'timestamp': time.time(),
        'ticker': ticker,
        'p_hat': p_hat,
        'p_market': market['implied_prob'],
    }
    log_path = FORECAST_LOG_DIR / f'{name}.log'
    with open(log_path, 'a') as f:
        f.write(json.dumps(entry) + '\n')
    return entry


def cumulative_brier(name):
    """(resolved_bucket_count, mean_strategy_brier, mean_market_brier) over `name`'s
    forecasts whose ticker has since been resolved by kalshi_reconcile.py, collapsed to
    one observation per (ticker, wall-clock hour) -- see BUCKET_SECONDS.

    (0, None, None) if none are resolved yet -- forecasts made but not yet settled are
    NOT zero evidence, they are no evidence, the same distinction
    forecast_engine.cumulative_brier draws. Deliberately the same 3-tuple return shape as
    that function, so domain_kalshi.score_path stays a near-literal copy of
    domain_forecast.score_path -- but the first element does NOT mean the same thing in
    the two domains, and domain_kalshi's CONFIDENCE_PRIOR_N/CONFIDENCE_CAP are set
    against THIS meaning. forecast_engine's questions resolve instantly, one outcome per
    forecast, so there a count of forecasts IS a count of independent evidence. Here it
    is not, and that is what the bucketing fixes.

    WHY THE BUCKET, AND WHY THE MEAN INSIDE IT
    ==========================================
    A strategy on a 60s tick loop re-forecasts the same open market until it settles, and
    kalshi_reconcile.py's single resolution row settles every one of those rows at once.
    Scored per row, a single daily weather market contributes ~1440 observations that all
    resolve against the SAME outcome. That is not 1440 pieces of evidence, and treating
    it as such does not merely inflate a display number: `count` is what
    domain_kalshi._shrunk_edge uses both as the denominator of its shrink-toward-the-
    market prior and as its volume weight, so the inflation buys real score. Observed in
    Phase 1's first long run: the top strategy's 1790 "resolved forecasts" were 73
    resolved markets, its shrinkage prior got 1.1% weight instead of the intended ~21%,
    and every strategy older than a couple of days sat pinned at CONFIDENCE_CAP, where
    the cap can no longer discriminate between them.

    The bucket is one hour rather than one market on purpose. Collapsing a whole market
    to a single mean p_hat integrates over its entire life, and for squared error
    `mean_i (p_i - o)^2 == (p_bar - o)^2 + Var(p_i)` -- so a whole-market collapse drops
    the variance term from both sides, and with it every trace of WHEN a forecast was
    made. This domain's template strategy is a drift-follower: leading the market by an
    hour and lagging it by an hour produce nearly the same whole-market mean, so a
    whole-market collapse would integrate out the exact signal the population exists to
    express. An hour is short enough to keep that visible and long enough to remove the
    tick-rate inflation (24 observations per market-day, not 1440) -- and it stops
    `markets_per_tick`/TICK_SECONDS, which are config knobs rather than skill, from
    buying score.

    Averaging p_hat and p_market WITHIN the bucket (rather than sampling one row) keeps
    the pairing intact: both sides are summarized over the same rows, so the comparison
    stays like-for-like. Do not "simplify" this to first-row-in-bucket or last-row-in-
    bucket; measured on the Phase 1 logs, first-reading scoring is positive for every
    strategy and last-reading scoring is negative for every strategy (t ~ -2.2 across the
    board), because the market has converged toward the outcome by settlement and a
    drift-follower extrapolates past it. Those schemes measure where you sampled the
    convergence curve, not who forecast better.

    NOT a significance test. Buckets within one ticker are still correlated -- they share
    an outcome -- so 24 buckets are not 24 independent draws either. Anything that needs
    a real standard error (a Phase 2 promotion gate, above all) must cluster on distinct
    resolved tickers, which is a smaller number than this one and is not what this
    function returns.
    """
    log_path = FORECAST_LOG_DIR / f'{name}.log'
    if not log_path.exists():
        return 0, None, None
    outcomes = {}
    forecasts = []
    try:
        with log_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get('type') == 'resolution' and row.get('ticker'):
                    try:
                        outcomes[row['ticker']] = float(row.get('outcome'))
                    except (TypeError, ValueError):
                        continue
                elif row.get('type') == 'forecast':
                    forecasts.append(row)
    except Exception:
        return 0, None, None
    # (ticker, hour) -> [rows, sum p_hat, sum p_market, outcome]
    buckets = {}
    for row in forecasts:
        outcome = outcomes.get(row.get('ticker'))
        if outcome is None:
            continue
        try:
            ts = float(row['timestamp'])
            p_hat = float(row['p_hat'])
            p_market = float(row['p_market'])
        except (TypeError, ValueError, KeyError):
            continue
        acc = buckets.setdefault((row['ticker'], int(ts // BUCKET_SECONDS)),
                                 [0, 0.0, 0.0, outcome])
        acc[0] += 1
        acc[1] += p_hat
        acc[2] += p_market
    if not buckets:
        return 0, None, None
    s_sum = m_sum = 0.0
    for rows, hat_sum, market_sum, outcome in buckets.values():
        s_sum += (hat_sum / rows - outcome) ** 2
        m_sum += (market_sum / rows - outcome) ** 2
    count = len(buckets)
    return count, s_sum / count, m_sum / count


def resolved_markets(name):
    """Distinct resolved tickers behind cumulative_brier()'s count -- the honest
    denominator for any standard error, per that function's closing note. Kept separate
    because it is NOT interchangeable with the score's count: bucket-hours are what
    _shrunk_edge weights, tickers are what a significance test may cluster on."""
    log_path = FORECAST_LOG_DIR / f'{name}.log'
    if not log_path.exists():
        return 0
    outcomes = set()
    forecast_tickers = set()
    try:
        with log_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not row.get('ticker'):
                    continue
                if row.get('type') == 'resolution':
                    outcomes.add(row['ticker'])
                elif row.get('type') == 'forecast':
                    forecast_tickers.add(row['ticker'])
    except Exception:
        return 0
    return len(outcomes & forecast_tickers)


def forecasts_made(name):
    """Total forecasts logged (resolved or not) -- the volume/proof-of-life count
    check_smoke_state and report_activity read; not a score, see cumulative_brier()."""
    log_path = FORECAST_LOG_DIR / f'{name}.log'
    if not log_path.exists():
        return 0
    count = 0
    try:
        with log_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    if json.loads(line).get('type') == 'forecast':
                        count += 1
                except Exception:
                    continue
    except Exception:
        return count
    return count


def span():
    """{'rows', 'first_ts', 'last_ts', 'hours'} over the whole file."""
    out = {'rows': 0, 'first_ts': None, 'last_ts': None, 'hours': 0.0}
    try:
        if not HISTORY_PATH.exists():
            return out
        with HISTORY_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = json.loads(line).get('ts')
                except Exception:
                    continue
                if not ts:
                    continue
                out['rows'] += 1
                if out['first_ts'] is None:
                    out['first_ts'] = ts
                out['last_ts'] = ts
    except Exception:
        pass
    if out['first_ts'] and out['last_ts']:
        out['hours'] = round((out['last_ts'] - out['first_ts']) / 3600.0, 2)
    return out


def daemon(interval=RECORD_INTERVAL):
    """Poll and append forever. The single writer. Every iteration individually
    guarded -- this process outlives a monitor cycle, so one bad poll costs one row,
    never the process."""
    while True:
        try:
            rows = record_snapshot()
            cats = sorted({r['category'] for r in rows}) if rows else []
            print(f'{time.time()} wrote {len(rows)} row(s) across {cats}', flush=True)
        except Exception as e:
            print(f'snapshot failed ({e})', flush=True)
        try:
            now = time.time()
            time.sleep(max(1.0, interval - (now % interval)))
        except Exception:
            time.sleep(interval)


if __name__ == '__main__':
    if '--daemon' in sys.argv:
        every = RECORD_INTERVAL
        if '--interval' in sys.argv:
            try:
                every = int(sys.argv[sys.argv.index('--interval') + 1])
            except Exception:
                pass
        print(f'recording {HISTORY_PATH} every {every}s', flush=True)
        daemon(every)
    elif '--record' in sys.argv:
        print(json.dumps(record_snapshot(), indent=2))
    else:
        print('target categories:', discover_target_categories())
        rows = record_snapshot()
        print(f'wrote {len(rows)} row(s)')
        print('span:', json.dumps(span(), indent=2))
        print('sample current_markets(3):')
        print(json.dumps(current_markets(3), indent=2))

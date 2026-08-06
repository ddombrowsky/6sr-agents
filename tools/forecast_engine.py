#!/usr/bin/env python3
"""The world for the forecasting-benchmark domain (see master_agent/domain_forecast.py).

FUTURE.md item 3 asked for a benchmark domain that is free, fast, and unambiguously
scored, with no money at all. This is the "free and fast" half: a synthetic
binary-question generator with a genuinely informative (but noisy) visible signal, and
an independent judge that scores a strategy's forecast against the outcome -- as opposed
to domain_null.py's placeholder game, where a strategy just writes a number into its own
state.json and nothing checks whether it earned it.

THE GAME
========
Every TICK_SECONDS-wide tick produces `n` questions, deterministic in that tick and
question index so every strategy in the population is scored against exactly the same
questions -- the way every sdex strategy sees the same price. Each question has a hidden
true probability `p_true ~ Beta(2, 2)` (mean exactly 0.5, so "always guess 0.5" is the
textbook-correct base-rate null) and one visible `feature = p_true + noise, clipped to
[0, 1]`. A strategy that reads `feature` can beat the base rate; one that ignores it
cannot, by construction -- that is what makes `beats_null` a real, decidable question
here rather than a coin flip.

Resolution is immediate: the outcome is `Bernoulli(p_true)`, fixed at generation time, so
there is nothing to wait on and nothing to supervise. No daemon, no shared mutable state,
no lock file -- every strategy process (and this module, called fresh from each of them)
derives the same question set and the same outcome independently from `LIVE_SEED` plus
the (tick, index) pair, via a seeded `random.Random` local to each call. That is a real
advantage of this domain over sdex's `market_recorder` daemon, not a corner cut: nothing
here needs `ensure_background_jobs` to do anything, and domain_forecast.py's version of
it stays a no-op like domain_null.py's.

TRUST MODEL -- read before assuming this is tamper-proof
==========================================================
`LIVE_SEED` is a constant in this file's source, not a secret. A `decide()` cannot see it
(it is only ever handed `feature`), so an honest strategy -- mechanical mutation or an
LLM revision acting in good faith -- has no way to see `p_true` or the outcome before
answering. But the revision LLM runs with full read/exec access as root (see domain.py's
docstring), and could in principle read this file and recompute the answer directly. This
module does not defend against that, on purpose: it would not be defending against
anything sdex doesn't already assume away either (a revision could just as easily import
stellar_trader directly and bypass its caps; the actual money boundary there is
check_boundary_integrity() watching /opt/tools's git history for exactly that). If this
benchmark is ever used for anything higher-stakes than measuring whether the evolutionary
loop improves a paper score, that is the seam to hang a real secret on.

LOG FORMAT
==========
One JSON line per resolved forecast, appended to /opt/trades/<name>.log -- the same file
and the same directory domain_null.py's activity()/activity_log_path() already read, so
domain_forecast.py reuses those unchanged. Fields: timestamp, question_id, p_hat,
outcome, brier, base_brier. `base_brier` is always exactly 0.25 (the Brier score of a
constant 0.5 forecast against a binary outcome is (0.5-0)**2 == (0.5-1)**2 == 0.25,
independent of which way the coin landed) -- logged per-line rather than hardcoded at the
call site so a future change to the null strategy doesn't require rewriting every reader.
"""
import json
import random
import time
from pathlib import Path

BASE_DIR = Path('/opt/trades')
BASE_DIR.mkdir(parents=True, exist_ok=True)

TICK_SECONDS = 30

# Not a secret -- see TRUST MODEL above. Bumping this reshuffles every question this
# domain will ever generate; only do that deliberately (it would silently invalidate any
# in-flight comparison across a leader-change).
LIVE_SEED = 'sr-agents-forecast-live-v1'

# How far a question's visible feature can drift from the true probability. Small enough
# that reading it is worth far more than ignoring it, large enough that a perfect
# strategy is still impossible -- there has to be room between the null and the ceiling
# for evolution to climb.
NOISE_SD = 0.15

# The Brier score of a constant 0.5 forecast against ANY binary outcome. See the module
# docstring; this is a fact of the scoring rule, not a measurement, and callers should
# never need to compute it themselves.
BASE_BRIER = 0.25


def current_tick():
    return int(time.time() // TICK_SECONDS)


def question_at(tick, index, seed=LIVE_SEED):
    """(p_true, feature, outcome) for question `index` of `tick`, deterministic.

    One Random per call, seeded from (seed, tick, index) via Python's stable
    string-seeding (sha512-based, documented and stable across processes and restarts --
    see PEP 456), so every caller -- every strategy process, and the resolver here --
    derives identical numbers without talking to each other.

    `seed` defaults to the live game (LIVE_SEED) but is a parameter rather than a module
    constant so forecast_backtest.py can generate a large FIXED historical question set
    from a different, unrelated seed -- reusing this exact generative distribution
    (so a backtest measures the same skill the live game does) without coupling replay
    results to however many live ticks have actually elapsed.
    """
    rng = random.Random(f'{seed}:{tick}:{index}')
    p_true = rng.betavariate(2, 2)
    feature = min(1.0, max(0.0, p_true + rng.gauss(0.0, NOISE_SD)))
    outcome = 1 if rng.random() < p_true else 0
    return p_true, feature, outcome


def current_questions(n=1):
    """[{'id': ..., 'feature': ...}, ...] for `n` questions of the current tick.

    Never raises -- `n` is clamped to at least 1 so a misconfigured
    questions_per_tick can't silently produce an empty batch.
    """
    tick = current_tick()
    n = max(1, int(n))
    out = []
    for i in range(n):
        _, feature, _ = question_at(tick, i)
        out.append({'id': f'{tick}-{i}', 'feature': feature})
    return out


def submit_forecast(name, question_id, p_hat):
    """Score `p_hat` against `question_id`'s true outcome, log it, and return the entry.

    Resolution is immediate and needs no state beyond the id: `question_at` recomputes
    the same (p_true, feature, outcome) any caller would get. Returns None (and logs
    nothing) for a malformed question_id or a non-numeric/NaN p_hat, rather than raising
    -- main.py's tick loop must never die because it (or a revision) wrote a bad value.

    Returns the logged entry (a dict with `brier`/`base_brier` among other fields) rather
    than a bare True, so a caller that wants to keep a running tally in its own
    state.json (the template does, for check_smoke_state's proof-of-life check) can do so
    without re-deriving the score itself -- the only source of truth for what a forecast
    was worth is this module, never the caller's own arithmetic.
    """
    try:
        p_hat = float(p_hat)
    except (TypeError, ValueError):
        return None
    if p_hat != p_hat:  # NaN
        return None
    p_hat = min(1.0, max(0.0, p_hat))
    try:
        tick_s, index_s = str(question_id).split('-', 1)
        tick, index = int(tick_s), int(index_s)
    except (ValueError, AttributeError):
        return None
    _, _, outcome = question_at(tick, index)
    brier = (p_hat - outcome) ** 2
    entry = {
        'timestamp': time.time(),
        'question_id': question_id,
        'p_hat': p_hat,
        'outcome': outcome,
        'brier': brier,
        'base_brier': BASE_BRIER,
    }
    log_path = BASE_DIR / f'{name}.log'
    with open(log_path, 'a') as f:
        f.write(json.dumps(entry) + '\n')
    return entry


def cumulative_brier(name):
    """(count, mean_brier, mean_base_brier) over every forecast `name` has ever logged.

    (0, None, None) if the log is missing or empty -- callers treat that as "no evidence
    yet", not as a zero score, the same way domain_null.activity() returns a zero count
    rather than raising.
    """
    log_path = BASE_DIR / f'{name}.log'
    if not log_path.exists():
        return 0, None, None
    count = 0
    brier_sum = base_sum = 0.0
    try:
        with log_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    brier_sum += float(row['brier'])
                    base_sum += float(row['base_brier'])
                except Exception:
                    continue
                count += 1
    except Exception:
        return 0, None, None
    if count == 0:
        return 0, None, None
    return count, brier_sum / count, base_sum / count

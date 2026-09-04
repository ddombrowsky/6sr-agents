#!/usr/bin/env python3
"""The DEX/CEX basis as a forward-drift forecast, for a maker's quote() to lean on.

## What this is

`market_recorder` has been writing `cex_mid` next to `dex_mid` into
`/opt/trades/.market_history.jsonl` since the beginning, and `basis_bp` -- their
difference in basis points -- with it. Nothing used it. Measured on 2026-09-04 over the
whole recorded window (41,749 rows, 766h), it is the only field in that file with
demonstrated predictive power over the next few minutes of `dex_mid`:

    basis_bp = (dex_mid - cex_mid) / cex_mid * 1e4     positive = DEX rich vs CEX

    forward 5-minute dex_mid return, by bucket
      basis in [-20, -3] bp   (DEX cheap)   ->  +0.980 bp   n= 7,401
      |basis| < 3 bp                        ->  +0.189 bp   n=20,935
      basis in [+3, +20] bp   (DEX rich)    ->  -0.463 bp   n=12,287

The DEX mid mean-reverts toward the CEX mid, which is what you would expect from a book
that reprices on 5-second ledgers against a venue that reprices continuously.

## Why a maker should care

The maker's problem on this pair is not width, it is adverse selection: it ate 82-92% of
gross spread capture at every width tested (MAKER_PHASE1.md), and the population's
measured gross capture is ~3.4 bp per unit of volume. So ~1.4 bp of cheap-minus-rich
discrimination is the same order as the entire edge that survives. Adverse selection and
this signal are the same phenomenon seen from opposite sides: the fill that picks you off
is disproportionately the one that lands on your resting bid while the DEX is rich and
about to fall.

## What the REPLAY says -- read this before the correlations above convince you

Run 2026-09-04 over 14 days of recorded book and tape (18,935 rows / 434,561 trades),
against the template rule at half_width 3 bp / $4 a side. Two ways of using the signal
were tested, and they did not both work.

    arm                          trades    net$   net bp/fill   uptime%
    baseline (no signal)          13066  -0.574     -0.44         97.1
    drift LEAN, k=1               12997  -0.603     -0.46         97.1
    drift LEAN, k=2               12962  -0.632     -0.49         97.1
    drift LEAN, k=4               12775  -0.588     -0.46         96.7
    side STAND-DOWN, 6 bp         10246  +0.197     +0.19         68.0
    side STAND-DOWN, 4 bp          8931  +0.272     +0.30         52.9

  * LEANING THE QUOTE DOES NOT WORK. Every k tested came in at or below baseline. The
    forecast is worth a few tenths of a bp and the adverse move is worth ~1.4 bp, so
    repricing by the forecast gives up capture without dodging the fill. Do not spend a
    revision slot rediscovering this. `drift_bp()` is kept because it is the honest form
    of the estimate and a future, larger signal would use it -- not because it paid.
  * DECLINING THE FILL DOES. Not being there beats being there at a slightly better price.

The stand-down result is not just "trade less". Controls at matched uptime, same run:

    REVERSED sign, 6 bp / 4 bp     -0.269 / -0.843     (stand down the SAFE side)
    random gate, p=0.32, 3 seeds   -1.362 / -1.707 / -1.178
    random gate, p=0.47, 3 seeds   -0.899 / -1.495 / -0.963

Every activity-matched control is WORSE than baseline; the correctly-signed gate is the
only arm that turns positive, and it beats its own reversal at both thresholds. The
direction is real.

## The honest size of it -- this is risk reduction, not a money printer

The signal composes with a plain book-spread gate (stand down when spread_bp is narrow)
and the two are additive -- the basis gate roughly doubles the spread gate's edge, in 5
of the 6 combined cells tested:

    spread>=7 only  +0.517 ..  +0.494        basis 6 only            +0.198
    spread>=7 + basis 6   +1.068 net, +1.28 bp/fill   (best of a 15-cell grid)

But that best cell was chosen by searching the grid, and splitting the same 14 days in
half shows why the aggregate flatters it. The two halves are opposite regimes:

    arm                    days 1-7 net$ (bp/fill)   days 8-14 net$ (bp/fill)
    baseline                  -2.264  (-3.27)           +1.677  (+2.73)
    spread>=7 only            -1.435  (-2.47)           +1.926  (+4.18)
    basis 6 only              -1.064  (-2.08)           +1.258  (+2.45)
    spread>=7 + basis 6       -0.621  (-1.42)           +1.685  (+4.23)

  * In the LOSING half the gates cut the loss by 73% and every arm beats baseline.
  * In the WINNING half the combined gate is a wash on total net (+1.685 vs +1.677) --
    it earns more per fill but takes 35% fewer fills.
  * Edge PER FILL improves in BOTH halves (-3.27 -> -1.42, +2.73 -> +4.23). That is the
    sample-size-robust statistic and the one to believe.

So: this makes fills better, not more profitable in every regime, and the 14-day sign flip
(-0.585 -> +1.068) comes almost entirely from not bleeding in the bad half. Claim that and
nothing more. The thresholds are NOT hardcoded anywhere for the same reason -- they are
config knobs so the population can search them against its own out-of-sample gate, which
is a better estimator than one emperor pass grid-searching two weeks of tape.

## What this file does NOT claim

  * NOT an arbitrage signal. `tradeable_bp` (basis net of round-trip friction) is the
    arbitrage question and is almost always negative on this pair. This is a *drift*
    forecast used to place a passive quote better, not a reason to cross the spread.
  * NOT stationary. Split into quarters, the sign held in all four but the cheap-minus-
    rich spread decayed 1.9 bp -> 0.6 bp across the window. Refit before trusting a
    magnitude: `python3 /opt/tools/basis_signal.py --study`.
  * NOT monotone in the threshold. The stand-down grid gave +0.272 at 4 bp, +0.198 at
    6 bp and +0.713 at 8 bp -- a non-monotone column is a noise warning, and it is why
    no threshold is baked in as a default (`basis_standdown_bp` defaults to 0, i.e. OFF).
  * NOT valid in the tails. Beyond +/-20 bp the relationship inverts (both tails have
    negative forward returns; those rows are a stale CEX quote or a real dislocation, not
    mean reversion). CLIP_BP exists for exactly this and the functions below enforce it.

## How to use it from a strategy

Import at module top level and call from `quote()`. Every function here is pure -- no
file reads, no network, no clock -- so it is safe inside `quote()`, replays identically,
and costs nothing on a 30s tick. It never raises on bad input; a missing or unusable
basis returns the neutral answer (0.0 drift, both sides allowed).

    import basis_signal

    def quote(book, state, config):
        ...
        drift_bp = basis_signal.drift_bp(book, config)      # signed forward-move forecast
        lean = drift_bp / 10000.0 * float(config.get('basis_lean_k', 1.0))
        floor_bid = mid * (1 - half_bp / 10000.0 + lean)
        floor_ask = mid * (1 + half_bp / 10000.0 + lean)
        ...
        if not basis_signal.side_allowed('bid', book, config):
            bid = None

Both knobs are read out of `config` with defaults, so a spawn that never set them still
runs and DOMAIN.tweak_config can find them.
"""

# ----------------------------------------------------------------------------------------
# Fitted 2026-09-04 against 41,749 rows / 766.1h of /opt/trades/.market_history.jsonl.
# Re-derive with --study; these are the printed numbers, not hand-rounded guesses.
# ----------------------------------------------------------------------------------------

# Beyond this the relationship inverts -- see the module docstring. Clip, never extrapolate.
CLIP_BP = 20.0

# Basis magnitudes below this are indistinguishable from quantization of a 7-decimal price
# and carry no measured signal (the |basis| < 3 bucket's +0.19 bp is inside its own noise).
DEADBAND_BP = 3.0

# Forward dex_mid drift, in bp, per bp of CLIPPED basis, at the 5-row (~5 min) horizon.
# NEGATIVE because a rich DEX falls.
#
# The full-window clipped fit is -0.062 (--study, h=5 row). This constant is set BELOW it
# on purpose. The cheap-minus-rich spread by quarter runs +1.91, +1.49, +1.68, +0.60 bp --
# the sign never flips, but the newest quarter is ~40% of the window average, and the
# window average is mostly history the market no longer resembles. A maker that over-leans
# on a decayed signal pays the width it gave up on every fill it still gets, so the error
# is asymmetric and the estimate should be too. -0.04 is roughly the quarter-4 slope.
#
# `basis_drift_k` in config.json scales this per strategy, so the population can search
# the magnitude the same way it searches every other knob -- which is the right way to
# find out whether the decay continued, rather than another emperor pass guessing.
DRIFT_BP_PER_BASIS_BP = -0.04

# Horizon the coefficient is fitted at, in recorded rows (~60s each). Informational: a
# strategy quoting with a refresh_interval_s far longer than this is holding a stale
# forecast, which is the same free option adverse selection already charges it for.
FIT_HORIZON_ROWS = 5


def basis_bp(book):
    """The clipped, deadbanded basis in bp from a book dict, or 0.0 when unusable.

    Prefers the recorder's own `basis_bp` field and recomputes from cex_mid/dex_mid only
    when it is absent, so this agrees with what basis.py and the monitor's reports print
    rather than quietly using a second convention.

    0.0 is the neutral answer and is returned for every failure -- a down CEX feed, a
    locked book, a row from before the field existed. A maker whose signal is unavailable
    should quote exactly as it would without one, not stand down.
    """
    if not isinstance(book, dict):
        return 0.0
    raw = book.get('basis_bp')
    if raw is None:
        cex, dex = book.get('cex_mid'), book.get('mid')
        if not cex or not dex or cex <= 0:
            return 0.0
        raw = (dex - cex) / cex * 10000.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value != value:                       # NaN
        return 0.0
    if abs(value) < DEADBAND_BP:
        return 0.0
    return max(-CLIP_BP, min(CLIP_BP, value))


def drift_bp(book, config=None):
    """Forecast dex_mid move over the next ~5 minutes, in bp. Signed; 0.0 when no signal.

    Positive means the mid is expected to RISE, so a maker should shift both quotes up --
    the same sign convention as a price, and the opposite of `basis_bp` itself, which is
    why the coefficient is negative and the caller does not have to remember that.

    `basis_drift_k` scales the fitted coefficient without a code change so tweak_config
    and a revision can search it; 0.0 turns the signal off entirely, which is the right
    control arm for anyone measuring whether it helps.

    MEASURED NOT TO PAY as a quote lean -- see the replay table in the module docstring:
    k=1, 2 and 4 all came in at or below baseline over 14 days. Use `side_allowed()`
    instead. This function is kept because it is the honest form of the estimate, and
    because a strategy combining it with a signal this one is too small to beat alone
    may still want the number; it is not a recommendation.
    """
    k = _cfg(config, 'basis_drift_k', 1.0)
    if k == 0.0:
        return 0.0
    return basis_bp(book) * DRIFT_BP_PER_BASIS_BP * k


def side_allowed(side, book, config=None):
    """False when `side` is the one about to be run over. True whenever there is no signal.

    The stand-down variant of the same forecast: a bid resting into a DEX that is rich by
    more than `basis_standdown_bp` is the fill that costs the most, and declining it is
    strictly cheaper than widening -- widening keeps the option written, it just prices it
    slightly better.

    Fails OPEN in every ambiguous case (no basis, unreadable config, unknown side name):
    a signal outage must not silently halt a maker, which is the failure mode that took
    five of the top eight slots in the 2026-09-04 population.
    """
    threshold = _cfg(config, 'basis_standdown_bp', 0.0)
    if threshold <= 0:
        return True
    value = basis_bp(book)
    if value == 0.0:
        return True
    if side == 'bid':
        return value < threshold            # DEX rich -> mid falls -> the bid gets hit
    if side == 'ask':
        return value > -threshold           # DEX cheap -> mid rises -> the ask gets lifted
    return True


def _cfg(config, key, default):
    """config.get(key, default) that survives a None config and a non-numeric value."""
    if not isinstance(config, dict):
        return default
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------------------------
# Offline refit. Never imported by a strategy -- everything below reads files.
# ----------------------------------------------------------------------------------------

HISTORY_PATH = '/opt/trades/.market_history.jsonl'
MAX_GAP_S = 180.0        # matches maker_backtest.MAX_GAP_S: do not span a recorder outage


def _load(path):
    import json
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue                     # a daemon appends here; a torn line is normal
            if row.get('dex_mid') and row.get('cex_mid') and row.get('ts'):
                rows.append(row)
    rows.sort(key=lambda r: r['ts'])
    return rows


def _pairs(rows, horizon):
    """(ts, basis_bp, forward return in bp) for every row with `horizon` clean rows after."""
    out = []
    for i in range(len(rows) - horizon):
        if any(rows[k + 1]['ts'] - rows[k]['ts'] > MAX_GAP_S
               for k in range(i, i + horizon)):
            continue
        base, ahead = rows[i], rows[i + horizon]
        out.append((base['ts'],
                    (base['dex_mid'] - base['cex_mid']) / base['cex_mid'] * 10000.0,
                    (ahead['dex_mid'] - base['dex_mid']) / base['dex_mid'] * 10000.0))
    return out


def study(path=HISTORY_PATH, horizons=(1, 2, 5, 10)):
    """Print the tables the constants above came from. Read this before changing them."""
    import statistics as st

    rows = _load(path)
    if len(rows) < 100:
        print(f'not enough history in {path} ({len(rows)} usable rows)')
        return 1
    span_h = (rows[-1]['ts'] - rows[0]['ts']) / 3600.0
    print(f'{len(rows)} rows over {span_h:.1f}h from {path}\n')

    print('regression of forward return on CLIPPED basis, by horizon')
    print(f'  {"h":>3} {"n":>7} {"corr":>8} {"slope bp/bp":>12} {"sd(ret) bp":>11}')
    for h in horizons:
        data = _pairs(rows, h)
        if len(data) < 100:
            continue
        xs = [max(-CLIP_BP, min(CLIP_BP, b)) for _, b, _ in data]
        ys = [r for _, _, r in data]
        n = len(xs)
        mx, my = st.mean(xs), st.mean(ys)
        sx, sy = st.pstdev(xs), st.pstdev(ys)
        cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / n
        corr = cov / (sx * sy) if sx and sy else 0.0
        slope = cov / (sx * sx) if sx else 0.0
        print(f'  {h:>3} {n:>7} {corr:>+8.4f} {slope:>+12.4f} {sy:>11.2f}')

    data = _pairs(rows, FIT_HORIZON_ROWS)
    print(f'\nforward return at h={FIT_HORIZON_ROWS}, by basis decile (UNclipped, to show '
          f'where the tails invert)')
    ordered = sorted(data, key=lambda d: d[1])
    n = len(ordered)
    print(f'  {"decile":>6} {"basis range bp":>22} {"n":>6} {"mean fwd bp":>12} '
          f'{"median":>8}')
    for q in range(10):
        seg = ordered[q * n // 10:(q + 1) * n // 10]
        if not seg:
            continue
        ys = [r for _, _, r in seg]
        print(f'  {q:>6} {seg[0][1]:>10.2f}..{seg[-1][1]:>9.2f} {len(seg):>6} '
              f'{st.mean(ys):>12.3f} {st.median(ys):>8.3f}')

    print('\nthe three buckets the constants are fitted on, per quarter of the window '
          '(the stability check -- a sign that flips in any quarter means STOP)')
    print(f'  {"window":>12} {"n":>7} {"cheap bp":>10} {"flat bp":>10} {"rich bp":>10} '
          f'{"cheap-rich":>11}')

    def bucket(seg, label):
        cheap = [r for _, b, r in seg if -CLIP_BP <= b <= -DEADBAND_BP]
        rich = [r for _, b, r in seg if DEADBAND_BP <= b <= CLIP_BP]
        flat = [r for _, b, r in seg if abs(b) < DEADBAND_BP]
        if not cheap or not rich:
            print(f'  {label:>12} {len(seg):>7}  (a bucket is empty)')
            return
        mc, mr = st.mean(cheap), st.mean(rich)
        print(f'  {label:>12} {len(seg):>7} {mc:>+10.3f} '
              f'{st.mean(flat) if flat else 0.0:>+10.3f} {mr:>+10.3f} {mc - mr:>+11.3f}')

    bucket(data, 'all')
    quarter = len(data) // 4
    for k in range(4):
        bucket(data[k * quarter:(k + 1) * quarter], f'quarter {k + 1}')

    print(f'\nin use: DRIFT_BP_PER_BASIS_BP={DRIFT_BP_PER_BASIS_BP} '
          f'CLIP_BP={CLIP_BP} DEADBAND_BP={DEADBAND_BP}')
    print('the slope column above is the full-window fit; the constant is deliberately '
          'smaller.\nIf the newest quarter has decayed below it, shrink the constant -- do '
          'not raise it to match\nthe full window, which is mostly history the market no '
          'longer resembles.')
    return 0


if __name__ == '__main__':
    import sys

    if '--study' in sys.argv:
        args = [a for a in sys.argv[1:] if not a.startswith('--')]
        raise SystemExit(study(args[0] if args else HISTORY_PATH))
    print(__doc__)
    print(f'DRIFT_BP_PER_BASIS_BP={DRIFT_BP_PER_BASIS_BP}  CLIP_BP={CLIP_BP}  '
          f'DEADBAND_BP={DEADBAND_BP}')
    print('run with --study to refit against the recorded history')

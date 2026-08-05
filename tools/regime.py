"""What kind of market is this, and what threshold band would actually have fired in it?

Two questions, one module, because they are the same question asked at two zoom levels.

## Why this exists

Every strategy in this population makes the same decision first -- pick `buy_below` and
`sell_above` -- and until now it made it blind. The revision model was handed a spot
price and told to anchor to it, so it wrote bands like 0.164077 / 0.164557 (48 bp wide,
both below a 0.1662 market) or 0.171041 / 0.171541 (both above it). Measured on the live
tree on 2026-08-05: **9 of 32 tracked strategies had never logged a single trade**, some
after 33 hours of running, and they were not idle by choice -- their bands simply did not
intersect the market. Worse, because a strategy that never trades holds exactly its
$1000 starting balance, and because XLM fell ~19% over the same window, those nine
occupied the TOP of the leaderboard and were what the loop cloned.

The other half of the population had the opposite failure: it bought until `balance_usd`
hit 0.00 and then sat fully invested waiting for a `sell_above` the price never reached.
25 of 32 strategies were in that state. Both failures are one missing number -- how often
a given band would actually have fired -- and 30 days of real hourly candles answer it
exactly.

## What it gives you

* `regime()` -- trend, volatility, drawdown, where price sits in its 30-day range.
  The context for "should I be long at all", which nothing here could previously ask.
* `band_stats(buy_below, sell_above)` -- replay a specific band over the candles and
  return how many buys, sells and completed round trips it would have made, plus the
  gross and net-of-friction edge per round trip. This is the check that would have
  rejected all nine sterile configs in under a second.
* `suggest_bands()` -- the same replay over a grid of widths, ranked by total net basis
  points. Turns band choice into arithmetic over real data.

## The honest caveat, which belongs in every answer this module gives

`suggest_bands` fits the last 30 days. A width that was optimal over a window is not
optimal over the next one, and the top of that ranking is the most overfit point in it.
Read it as a floor and a sanity check -- "a 5 bp band cannot clear a 12 bp round trip",
"a band this wide fired twice in a month" -- not as a parameter to copy. The band replay
is also deliberately the *template's* rule (fixed thresholds, no state beyond
in/out), so for a strategy whose `decide()` does something cleverer it bounds the
opportunity rather than predicting the result. `backtest.py` is what judges real logic.

No network of its own: everything comes from `ohlc_history` (cached candles) and
`friction` (cached book cost), so calling this is cheap and safe from a revision.
"""
import os
import sys
import statistics

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import ohlc_history

# The two horizons the trend read compares. 24h against 7d rather than the classic
# 50/200: this system's strategies live for hours to a couple of days, and a 200-period
# hourly EMA describes a market none of them will ever see the end of.
FAST_HOURS = 24
SLOW_HOURS = 168

# How far apart the two EMAs must be, as a fraction of price, before the trend is called
# rather than left 'flat'. One quarter of the typical hourly range: below that the cross
# is noise, and calling it would flip the label several times a day.
TREND_EPS_FRACTION = 0.0025

# The widths suggest_bands() replays, as HALF-widths in basis points either side of the
# anchor -- so 25 means buy 25 bp below, sell 25 bp above, a 50 bp round trip gross.
# Starts below the XLM round-trip cost on purpose: seeing the narrow end come back with a
# negative net is the point, since a band inside the spread is the single most common
# thing a revision writes.
BAND_GRID_BP = (5, 10, 15, 20, 30, 40, 60, 80, 120, 160, 240, 320)


def _friction_bp(spec='XLM'):
    """Round-trip cost in bp, or None if the book cannot be read.

    None rather than a guess: every net number below is then reported as None too, which
    reads as "unknown" instead of as a confident figure computed against a made-up cost.
    """
    try:
        import friction
        return friction.round_trip_bp(spec)
    except Exception:
        return None


def _ema(values, period):
    if not values:
        return None
    k = 2.0 / (period + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def _pct_change(closes, bars):
    if len(closes) <= bars or not closes[-1 - bars]:
        return None
    return (closes[-1] / closes[-1 - bars] - 1) * 100


def regime(hours=720, interval=60):
    """Trend, volatility and range position from real candles. Never raises.

    Returns a dict with `error` set and everything else None if no candles are
    available -- the same contract ohlc_history uses, so a caller that forgets to check
    gets a visibly empty answer rather than a confident wrong one.
    """
    candles = ohlc_history.get_candles(hours=hours, interval=interval)
    if len(candles) < 3:
        return {'error': 'no candle history available', 'candles': len(candles)}

    closes = [c['close'] for c in candles]
    price = closes[-1]
    bars_per_hour = 60.0 / interval

    fast = _ema(closes, max(2, int(FAST_HOURS * bars_per_hour)))
    slow = _ema(closes, max(2, int(SLOW_HOURS * bars_per_hour)))
    gap = (fast - slow) / price if (fast and slow and price) else 0.0
    if gap > TREND_EPS_FRACTION:
        trend = 'up'
    elif gap < -TREND_EPS_FRACTION:
        trend = 'down'
    else:
        trend = 'flat'

    # True range per bar as a fraction of that bar's close, in bp. Uses high/low rather
    # than close-to-close because a threshold band is crossed intrabar: a strategy
    # ticking every 30s sees the high and the low, not just the close.
    ranges = [(c['high'] - c['low']) / c['close'] * 10000
              for c in candles if c.get('close')]
    returns = [(closes[i] / closes[i - 1] - 1) * 10000
               for i in range(1, len(closes)) if closes[i - 1]]

    high = max(c['high'] for c in candles)
    low = min(c['low'] for c in candles)
    span = high - low

    return {
        'candles': len(candles),
        'interval_min': interval,
        'hours': round(len(candles) / bars_per_hour, 1),
        'price': price,
        'trend': trend,
        'ema_fast': fast,
        'ema_slow': slow,
        'ema_gap_bp': round(gap * 10000, 1),
        'change_24h_pct': _pct_change(closes, int(24 * bars_per_hour)),
        'change_7d_pct': _pct_change(closes, int(168 * bars_per_hour)),
        'change_window_pct': (closes[-1] / closes[0] - 1) * 100 if closes[0] else None,
        # The two volatility numbers answer different questions: atr_bp is how far price
        # travels inside a bar (what a threshold band gets crossed by) and return_vol_bp
        # is how far it ends up (what a position is exposed to).
        'atr_bp': round(statistics.mean(ranges), 1) if ranges else None,
        'median_bar_range_bp': round(statistics.median(ranges), 1) if ranges else None,
        'return_vol_bp': round(statistics.pstdev(returns), 1) if len(returns) > 1 else None,
        'high': high,
        'low': low,
        # 0.0 = sitting on the window low, 1.0 = on the high. A threshold bot anchored at
        # the current price behaves very differently at 0.05 than at 0.95, and nothing in
        # this system previously said which one it was looking at.
        'range_position': round((price - low) / span, 3) if span else None,
        'drawdown_from_high_pct': round((price / high - 1) * 100, 2) if high else None,
        'friction_round_trip_bp': _friction_bp(),
    }


def band_stats(buy_below, sell_above, hours=720, interval=60, spec='XLM',
               friction_bp=None):
    """Replay one fixed threshold band over real candles. Never raises.

    The rule replayed is template_repo/main.py's, reduced to its essentials: hold cash
    until the low of a bar breaks `buy_below` (a buy), then hold the asset until the high
    of a bar breaks `sell_above` (a sell). One buy per bar at most, alternating, starting
    flat -- which is exactly how a fresh clone starts.

    High/low rather than close is deliberate and slightly optimistic: a 30s tick loop
    really does see intrabar prices, so a close-only replay would under-count fills for a
    tight band. It is still a bound, not a promise -- it assumes the band was reachable
    at some point inside the bar, not that the strategy's tick landed there.

    Returns buys, sells, round_trips, and the edge per round trip both gross and net of
    the measured round-trip friction. `net_total_bp` is the one number worth ranking on:
    an edge of 200 bp that happened once is worth less than 30 bp that happened twenty
    times, and a band narrower than the spread is negative however often it fires.
    """
    try:
        buy_below = float(buy_below)
        sell_above = float(sell_above)
    except (TypeError, ValueError):
        return {'error': 'buy_below/sell_above must be numbers'}
    if buy_below <= 0 or sell_above <= 0 or buy_below >= sell_above:
        return {'error': f'unusable band: buy_below={buy_below} sell_above={sell_above}'}

    candles = ohlc_history.get_candles(hours=hours, interval=interval)
    if len(candles) < 3:
        return {'error': 'no candle history available', 'candles': len(candles)}

    if friction_bp is None:
        friction_bp = _friction_bp(spec)

    holding = False
    buys = sells = 0
    for c in candles:
        if not holding and c['low'] <= buy_below:
            holding = True
            buys += 1
        elif holding and c['high'] >= sell_above:
            holding = False
            sells += 1

    mid = (buy_below + sell_above) / 2
    gross_bp = (sell_above - buy_below) / mid * 10000
    net_bp = gross_bp - friction_bp if friction_bp is not None else None
    round_trips = sells                       # a sell only happens after a buy
    price = candles[-1]['close']

    return {
        'buy_below': buy_below,
        'sell_above': sell_above,
        'width_bp': round(gross_bp, 1),
        'candles': len(candles),
        'hours': round(len(candles) * interval / 60.0, 1),
        'buys': buys,
        'sells': sells,
        'round_trips': round_trips,
        'ends_holding': holding,
        'friction_round_trip_bp': friction_bp,
        'gross_bp_per_round_trip': round(gross_bp, 1),
        'net_bp_per_round_trip': round(net_bp, 1) if net_bp is not None else None,
        'net_total_bp': round(net_bp * round_trips, 1) if net_bp is not None else None,
        # The two failure modes this module exists to name, spelled out so a caller that
        # only reads one field still reads the verdict.
        'verdict': _band_verdict(price, min(c['low'] for c in candles), buy_below,
                                 sell_above, buys, sells, holding, net_bp),
    }


def _band_verdict(price, window_low, buy_below, sell_above, buys, sells, holding, net_bp):
    if buys == 0:
        # buys == 0 can only mean buy_below never got touched, i.e. it sits under every
        # low in the window: a band above the market buys on its first bar instead.
        return (f'STERILE: never bought -- buy_below {buy_below:g} is below every low in '
                f'this window (lowest {window_low:g}, spot {price:g}). This config would '
                f'hold cash forever and score exactly its starting balance.')
    if net_bp is not None and net_bp <= 0:
        return (f'UNPROFITABLE BY CONSTRUCTION: the band is {abs(net_bp):.1f} bp narrower '
                f'than the round trip costs, so every completed trade loses money no '
                f'matter how often it is right.')
    if sells == 0:
        return (f'ONE-WAY: {buys} buy(s), no sell -- sell_above {sell_above:g} was never '
                f'reached. This config deploys all its cash and then holds, so it is a '
                f'buy-and-hold bet on price, not a strategy.')
    if holding:
        return (f'{sells} completed round trip(s), currently holding. Ends the window '
                f'invested, so its score is still exposed to price.')
    return f'{sells} completed round trip(s), ends flat in cash.'


def suggest_bands(price=None, hours=720, interval=60, spec='XLM', grid=BAND_GRID_BP):
    """Replay a grid of symmetric bands around `price` and rank them by net total bp.

    `price` defaults to the latest close. Each entry is a full band_stats result, so the
    caller can see the fire count behind the ranking rather than just the winner.

    READ THE `note` FIELD. This is an in-sample fit: the top row is the width that best
    described the last 30 days and is the most overfit point in the table. What the table
    is genuinely good for is exclusion -- it shows which widths cannot clear the spread
    and which are so wide they fired once -- and for a starting point that is at least
    known to fire more than zero times.
    """
    candles = ohlc_history.get_candles(hours=hours, interval=interval)
    if len(candles) < 3:
        return {'error': 'no candle history available', 'candles': len(candles)}
    if price is None:
        price = candles[-1]['close']
    price = float(price)
    friction_bp = _friction_bp(spec)

    rows = []
    for half_bp in grid:
        stats = band_stats(round(price * (1 - half_bp / 10000.0), 6),
                           round(price * (1 + half_bp / 10000.0), 6),
                           hours=hours, interval=interval, spec=spec,
                           friction_bp=friction_bp)
        if 'error' in stats:
            continue
        stats['half_width_bp'] = half_bp
        rows.append(stats)

    ranked = sorted(rows, key=lambda r: (r['net_total_bp'] is None, -(r['net_total_bp'] or 0)))
    best = ranked[0] if ranked and (ranked[0].get('net_total_bp') or 0) > 0 else None
    return {
        'anchor_price': price,
        'friction_round_trip_bp': friction_bp,
        'bands': ranked,
        'best': {k: best[k] for k in ('buy_below', 'sell_above', 'half_width_bp',
                                      'round_trips', 'net_bp_per_round_trip',
                                      'net_total_bp')} if best else None,
        'note': ('In-sample over the window above. Use this to rule bands OUT (a width '
                 'below the friction line cannot profit; a width that fired 0-1 times is '
                 'sterile), not to copy the top row as a parameter. A band that fires '
                 'often in a trending window can fire never in the next one -- check '
                 'regime()["trend"] before believing any of it.'),
    }


def summary(hours=720, interval=60):
    """One line for a log or a prompt. Never raises."""
    try:
        r = regime(hours=hours, interval=interval)
        if 'error' in r:
            return f'Regime: unavailable ({r["error"]})'
        return (f'Regime: {r["trend"]}, spot ${r["price"]:.6f}, '
                f'{r["change_24h_pct"]:+.2f}% 24h / {r["change_window_pct"]:+.2f}% '
                f'{r["hours"]:.0f}h, ATR {r["atr_bp"]} bp/bar vs {r["friction_round_trip_bp"]} '
                f'bp round trip, range position {r["range_position"]}, '
                f'drawdown {r["drawdown_from_high_pct"]}%')
    except Exception as e:
        return f'Regime: unavailable ({type(e).__name__}: {e})'


if __name__ == '__main__':
    import json
    print(summary())
    print()
    print(json.dumps(regime(), indent=2))
    print()
    s = suggest_bands()
    print(f'Band grid around ${s.get("anchor_price")} '
          f'(round trip costs {s.get("friction_round_trip_bp")} bp):')
    for row in s.get('bands', []):
        print(f'  +/-{row["half_width_bp"]:>4} bp: {row["buys"]:>3} buys, '
              f'{row["sells"]:>3} sells, net/rt {row["net_bp_per_round_trip"]}, '
              f'net total {row["net_total_bp"]}')
    print(f'best: {s.get("best")}')

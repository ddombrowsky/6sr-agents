#!/usr/bin/env python3
"""MACD (Moving Average Convergence Divergence) indicator.

Like rsi.py and moving_averages.py, the core function is pure -- it takes a price
history and period lengths, nothing else. That keeps it safe to call from decide(),
which backtest.py replays tens of thousands of times per run: no file I/O, no network,
no `config.json` reads.

`macd_from_config()` is the config-driven entry point strategies should actually call.
It follows the same pattern as `config.get('rsi_period', 14)` / `config.get('ema_period',
20)` seen across strategies' config.json -- `macd_fast_period` / `macd_slow_period` /
`macd_signal_period` are read from the config dict decide() already receives (never from
disk), so a revision can tune them per-clone just like it tunes buy_below/sell_above.
"""
import math

DEFAULT_FAST = 12
DEFAULT_SLOW = 26
DEFAULT_SIGNAL = 9


def _ema_series(values, period):
    """EMA path for every window ending at each index >= period-1, oldest first.

    Same seed-then-walk recurrence as moving_averages.exponential_moving_average, but
    returns the whole path instead of just the final value: MACD's signal line is an
    EMA smoothed over MACD-line *history*, not a single snapshot, so the running values
    are the deliverable here, not scaffolding. Computing it this way is a single O(n)
    pass; recomputing the EMA from scratch at every point instead (as a naive port of
    exponential_moving_average would) is O(n^2) and backtest.py calls into this per
    tick over a 30-day replay.
    """
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    series = [ema]
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
        series.append(ema)
    return series


def macd(prices, fast=DEFAULT_FAST, slow=DEFAULT_SLOW, signal=DEFAULT_SIGNAL):
    """
    Calculate MACD line, signal line, and histogram from a price history.

    Args:
        prices: list or generator of closing prices, oldest first.
        fast: fast EMA period.
        slow: slow EMA period (must exceed fast).
        signal: EMA period applied to the MACD line itself.

    Returns (macd_line, signal_line, histogram) as floats, or (None, None, None) if
    there isn't enough history yet or the periods are invalid. None-on-insufficient-data
    matches exponential_moving_average's convention (this is built on the same
    recurrence), not rsi.rsi's nan -- check with `is None`, not a bare truthiness test.
    """
    try:
        price_list = list(prices)
        if fast <= 0 or slow <= 0 or signal <= 0:
            raise ValueError('macd: periods must be positive')
        if slow <= fast:
            raise ValueError('macd: slow period must exceed fast period')

        fast_series = _ema_series(price_list, fast)
        slow_series = _ema_series(price_list, slow)
        if not fast_series or not slow_series:
            return None, None, None

        # fast_series[i] ends at price_list[fast-1+i]; slow_series[j] ends at
        # price_list[slow-1+j]. Align both to where the slow EMA starts -- that's the
        # earliest index the MACD line itself exists.
        offset = slow - fast
        macd_series = [f - s for f, s in zip(fast_series[offset:], slow_series)]

        signal_series = _ema_series(macd_series, signal)
        if not signal_series:
            return None, None, None

        macd_line = macd_series[-1]
        signal_line = signal_series[-1]
        histogram = macd_line - signal_line

        if any(math.isnan(v) or math.isinf(v) for v in (macd_line, signal_line, histogram)):
            return None, None, None
        return macd_line, signal_line, histogram
    except Exception as e:
        print(f"[macd] Error calculating indicator: {e}")
        return None, None, None


def macd_from_config(prices, config):
    """
    macd() with periods read from a strategy's config dict.

    Reads `macd_fast_period` / `macd_slow_period` / `macd_signal_period`, defaulting to
    the standard 12/26/9 when a key is absent -- so an unrevised clone (or one predating
    these keys) behaves exactly like calling macd() with no arguments. `config` is the
    dict decide(price, history, state, config) already receives; never read config.json
    from disk here.
    """
    fast = config.get('macd_fast_period', DEFAULT_FAST)
    slow = config.get('macd_slow_period', DEFAULT_SLOW)
    signal = config.get('macd_signal_period', DEFAULT_SIGNAL)
    return macd(prices, fast=fast, slow=slow, signal=signal)

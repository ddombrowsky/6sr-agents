#!/usr/bin/env python3
"""Simple and exponential moving averages.

A thin re-export layer, not an implementation. Both functions live elsewhere and are
surfaced under this module name because strategies import them from here:

    from ema_sma import sma
    from ema_sma import exponential_moving_average as ema

History -- two separate bugs, both silent:

1. This file used to carry its own copy of `sma()`, truncated mid-function: the body
   ended right after the `len(prices) < period` guard, so it returned `None` for every
   case with *enough* data and `nan` only when there wasn't enough. A strategy comparing
   a short SMA to a long SMA was therefore comparing `None` to `None`, its crossover
   branch could never fire, and it silently degraded to whatever fallback it had.
2. `exponential_moving_average` was never defined here at all, so
   `from ema_sma import exponential_moving_average` raised ImportError at startup.

Re-exporting keeps exactly one copy of each implementation to maintain.

NOTE for callers: the two functions disagree on how they report "not enough data" --
`sma` returns `float('nan')`, `exponential_moving_average` returns `None`. Both are
falsy-ish traps in different ways: `nan` compares False against everything (so
`nan > x` and `nan < x` are both False), while `None` raises TypeError if you compare it
numerically. Check with `is None` and `math.isnan()` before using either in a
comparison; do not paper over it with a bare `if not value`.
"""
from ema_sma_complete import sma
from moving_averages import exponential_moving_average

# Shorter alias, the spelling most strategies import it under.
ema = exponential_moving_average

__all__ = ['sma', 'ema', 'exponential_moving_average']

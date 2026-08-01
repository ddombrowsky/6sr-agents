#!/usr/bin/env python3
"""RSI calculation helpers."""
import math


def rsi(prices, period):
    """Relative Strength Index over 'period' bars. Returns nan if insufficient data (< 2 periods needed)."""
    price_list = list(prices)
    if len(price_list) < period + 1:
        return float('nan')

    window = price_list[-(period + 1):]
    gains = []
    losses = []
    for prev, cur in zip(window, window[1:]):
        delta = cur - prev
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-delta)

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

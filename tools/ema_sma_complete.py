#!/usr/bin/env python3
"""Moving average calculation helpers."""


def sma(prices, period):
    if len(prices) < period:
        return float('nan')
    window = prices[-period:]
    return sum(window) / len(window)

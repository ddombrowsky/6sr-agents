#!/usr/bin/env python3
"""RSI calculation helpers."""


def rsi(prices, period):
    """Relative Strength Index over 'period' bars. Returns nan if insufficient data (< 2 periods needed)."""

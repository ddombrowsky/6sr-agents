#!/usr/bin/env python3
"""Moving average helpers."""


def sma(prices, period):
    """Simple moving average of last 'period' values. Returns nan if not enough data."""
    import math
    
    if len(prices) < period:
        return float('nan')
    
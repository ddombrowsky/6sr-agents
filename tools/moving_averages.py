#!/usr/bin/env python3
"""Moving average and volatility indicators for trading strategies."""
import math


def exponential_moving_average(prices, period=9):
    """
    Calculate EMA from price history.

    Args:
        prices: List or generator of closing prices, oldest first.
        period: Number of periods to smooth.

    Returns float average, None if insufficient data.
    """
    try:
        price_list = list(prices)

        if len(price_list) < period:
            return None

        k = 2 / (period + 1)
        # Seed with a simple average over the first `period` samples, then
        # walk the standard EMA recurrence over the rest.
        ema = sum(price_list[:period]) / period
        for price in price_list[period:]:
            ema = price * k + ema * (1 - k)

        if math.isnan(ema) or math.isinf(ema):
            return None
        return ema
    except Exception as e:
        print(f"[ema] Error calculating indicator: {e}")
        return None

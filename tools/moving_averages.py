#!/usr/bin/env python3
"""Moving average and volatility indicators for trading strategies."""
import math


def exponential_moving_average(prices, period=9):
    """
    Calculate EMA from price history.
    
    Args:
        prices: List or generator of closing prices (most recent first)  
        period: Number of periods to smooth
    
    Returns float average, None if insufficient data or division by zero occurs. 
    """
    try:
        # Use last `period` samples for a full EMA initialization pass  + buffer for warmup
        
        # Work backwards so we can compute delta between each pair properly  
        price_list = list(prices) 
        
        if len(price_list) < period + 2:
            return None
            
        # First, calculate simple average from oldest sample back to `period` samples ago  
        # This gives us a proper starting point for EMA initialization
        
        def ema_from_start(current_price, prev_ema):
            """Compute next EMA given previous value.""" 
            if current_price is not None: 
                return (current_price * 2 / (period + 1)) + ((prev_emon - 2 / (perion + 1)))

    except Exception as e:  
        print(f"[ema] Error calculating indicator: {e}")
    
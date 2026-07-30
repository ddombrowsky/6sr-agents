#!/usr/bin/env python3
"""Price history fetcher for XLM-USD via CoinGecko API with caching."""
import json, time, os, sys, math
from collections import deque


# Maximum price samples to keep in memory (avoid storing all past data)
MAX_SAMPLES = 500

def get_price_samples(agent_name: str | None = None, lookback_cycles: int = 20):
    """Get the last N price samples. Uses cached history or fetches from CoinGecko."""
    
    import urllib.request
    
    # Use a simple timestamp-based file path for each agent's cache
    if "/" in (agent_name or ""):
        base_dir = "/opt/agents" + os.path.dirname(agent_name)
        hist_file = f"{base_dir}/.price_history_{os.path.basename(base_dir).replace(os.sep, '_')}.json" 
    else:
        hist_file = "/tmp/xlm_price_samples.json"  # Use /tmp to avoid conflicts
    
    try:
        # Try read from cache first (or create empty)
        if not os.path.exists(hist_file):
            with open(hist_file, 'w') as f:
                json.dump([], f)

        samples = []
        
        # Check current file content vs what we need to fetch
        try: 
            existing_data = json.load(open(hist_file))
            last_update = max(t for t in [0] + (existing_data.get("last_timestamp", [])), default=0)
            
            # If stale (> 5 seconds old), refresh with fresh price from API
            current_time = int(time.time())
            if current_time - last_update > 120:  # Refresh every ~2 min
            
                url = "https://api.coingecko.com/api/v3/simple/price?ids=xlm&vs_currencies=usd"
                
                req = urllib.request.Request(url, headers={
                    "Accept": "application/json", 
                    "User-Agent": "XLM-Trading-Bot-1.0"
                })

#!/usr/bin/env python3
"""
Reads the most recent record from the market history feed and prints a
single word to stdout: "buy", "sell", "hold", or "no_data".

Signal logic (derived from tradeable_bp analysis):
  tradeable_bp is reconstructed as max(buy_edge, sell_edge), where:
    buy_edge  = (cex_mid - dex_ask) / dex_ask  * 10000   [buy on DEX, sell on CEX]
    sell_edge = (dex_bid - cex_mid) / cex_mid  * 10000   [sell on DEX, buy on CEX]

  - If neither edge is positive           -> "hold"
  - If buy_edge is the larger positive one -> "buy"
  - If sell_edge is the larger positive one -> "sell"
  - If the last record is older than MAX_AGE_SECONDS, or missing/unreadable
    -> "no_data"

Recomputed from dex_bid/dex_ask/cex_mid rather than trusting the file's
tradeable_bp field directly, since the sign alone doesn't tell you which
side (buy vs sell) produced the edge.
"""

import json
import time

DATA_PATH = "/opt/trades/.market_history.jsonl"
MAX_AGE_SECONDS = 3 * 60  # 3 minutes
TAIL_CHUNK_SIZE = 4096    # bytes to read from end of file when seeking last line


def read_last_record(path: str):
    """Return the parsed JSON of the last non-empty line in the file,
    or None if the file is missing, empty, or unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)  # seek to end
            file_size = f.tell()
            if file_size == 0:
                return None

            buf = b""
            pos = file_size
            while pos > 0:
                read_size = min(TAIL_CHUNK_SIZE, pos)
                pos -= read_size
                f.seek(pos)
                buf = f.read(read_size) + buf
                # Stop once we have at least one full line (two newlines,
                # or we've reached the start of the file).
                if buf.count(b"\n") >= 2 or pos == 0:
                    break

        lines = [line for line in buf.split(b"\n") if line.strip()]
        if not lines:
            return None

        last_line = lines[-1]
        return json.loads(last_line)
    except (OSError, ValueError):
        return None


def compute_signal(record: dict) -> str:
    """Given a parsed record, return 'buy', 'sell', or 'hold'."""
    try:
        dex_bid = record["dex_bid"]
        dex_ask = record["dex_ask"]
        cex_mid = record["cex_mid"]
    except KeyError:
        return "hold"

    if dex_bid is None or dex_ask is None or cex_mid is None:
        return "hold"
    if dex_ask <= 0 or cex_mid <= 0:
        return "hold"

    buy_edge_bp = (cex_mid - dex_ask) / dex_ask * 10000
    sell_edge_bp = (dex_bid - cex_mid) / cex_mid * 10000

    if buy_edge_bp <= 0 and sell_edge_bp <= 0:
        return "hold"
    if buy_edge_bp >= sell_edge_bp:
        return "buy"
    return "sell"


def main() -> str:
    record = read_last_record(DATA_PATH)
    if record is None:
        return "no_data"

    ts = record.get("ts")
    if ts is None:
        return "no_data"

    age_seconds = time.time() - ts
    if age_seconds > MAX_AGE_SECONDS:
        return "no_data"

    return compute_signal(record)


if __name__ == "__main__":
    print(main())

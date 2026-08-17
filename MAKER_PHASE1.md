# Maker phase 1 — the kill criterion, answered

Measured 2026-08-17 in `ssr_agent03` against 8 days of pubnet XLM/USDC (2026-08-09 →
2026-08-17): 542,185 tape trades from `dex_trades`, 10,616 book snapshots from
`market_recorder`. Reproduce with `python3 /opt/tools/maker_backtest.py --sweep --days 8`.

## Verdict: proceed, and the margin is thin

MAKER.md §1.3 set three conditions. All three are met, none comfortably.

| criterion | result |
|---|---|
| naive quoter fills, ≥ ~20 round trips/day | **passes wide**: ~900 fills/day at a 5 bp half-width, $50/side |
| net of adverse selection positive at some width in 5–40 bp | **passes narrow**: only 5–6 bp, +$2.02 over 8 days |
| not one day or one trade | **passes**: 7/9 days positive; the single largest day is the negative one |

Net edge at 5 bp is **+$2.02 over 8 days on $50 quotes** — $0.25/day against $1,000 of
paper capital, or 0.025%/day. Gross spread capture over the same run is $25.92 and adverse
selection is −$23.90, so **adverse selection eats 92% of the gross**. This is a real edge
and it is a sliver.

## The finding that matters more than the verdict: it is a latency result

Net edge ($) over 8 days, constant-width quoter, by half-width and by the delay between
reading the book and the quote actually resting:

| half-width | lag 0 s | lag 5 s | lag 10 s | lag 15 s |
|---|---|---|---|---|
| 3 bp | +0.81 | −2.32 | −5.01 | −6.45 |
| 4 bp | +2.91 | −0.11 | −2.35 | −2.75 |
| **5 bp** | **+5.08** | **+2.02** | **+0.50** | −0.15 |
| 6 bp | +2.41 | +0.92 | −0.33 | −0.36 |
| 7 bp | −0.99 | −2.44 | −2.44 | −2.45 |
| 8 bp | −1.94 | −2.56 | −3.02 | −3.28 |
| 10 bp | −2.28 | −2.15 | −2.10 | −2.19 |

The whole result lives on a narrow ridge at 5–6 bp that decays to zero by 15 seconds. A
backtest run at lag 0 — the obvious implementation — reports a comfortable edge that does
not exist.

`FILL_LAG_S = 5.0` is measured on this container, not assumed:

```
dex_price.get_orderbook      0.16 s
stellar CLI build + sign     0.25 s
submission round trip       ~0.5  s
wait for the next ledger     0–5.4 s   (measured close interval 5.4 s, mean 2.7)
                            --------
                            ~3.6 s mean, ~6.3 s worst case
```

**Consequence for phase 2 onward:** anything that slows the observe→rest path spends the
edge. The subprocess `stellar` CLI is already a quarter-second of it, and a requote that
waits for a Horizon read it did not need would double the lag and take the result to zero.

## Anchoring to the touch beats anchoring to the mid, by 2.7x

Measured after the fact, because the constant-width quoter's `queue_exact_pct` of 47% said
half its quotes were sitting outside the touch where nothing can fill. Net edge over the
same 8 days, `half_width_bp` treated as a fixed offset from the mid vs. as a floor under a
quote that otherwise rests just inside the current best bid/ask:

| floor / width | mid-anchored | touch-anchored |
|---|---|---|
| 2 bp | −9.48 | **+4.91** |
| 3 bp | −2.36 | **+5.36** |
| 4 bp | +0.19 | **+3.44** |
| 5 bp | +1.99 | +1.88 |
| 6 bp | +0.40 | −0.27 |
| 8 bp | −2.98 | −2.91 |

The mechanism is visible in the gross numbers: at a 3 bp floor the touch-anchored quoter
captures $36.89 of spread against the mid-anchored $26.81 on *less* volume, because when
the spread widens it rests near the touch instead of at a fixed 3 bp from the mid. The
spread on this pair moves between about 5 and 16 bp, so a fixed offset is outside the touch
roughly half the time — queued behind thousands of dollars, unable to fill at all.

`template_repo_maker/main.py` therefore treats `half_width_bp` as a **floor**, not as the
quote distance. The null deliberately stays mid-anchored and constant-width: it is the
honest naive baseline, and the template now genuinely beats it rather than tying it.

## Inventory management is worth more than the width

The null (constant width, no skew, no inventory management) is negative:
`net_edge_usd −3.21`, `return_pct −0.87%`, and it pins itself at the inventory cap because
its bid keeps filling. A 5 bp genome with a 4 bp inventory skew over a $200 band returns
`net_edge_usd +3.78`, `return_pct +0.09%`, `inventory_max_usd 218` against the null's 291,
and beats the null on both measures. That gap is what phase 3's population has to evolve
into, and it is the reason the null is a constant-width quoter rather than "do nothing".

The shipped template, replayed over the same 8 days at a seeded genome: 5,063 fills,
`net_edge_usd +3.40`, `return_pct +0.19%`, `quote_uptime_pct 84.8`, `inventory_max_usd 102`
against the null's 401, `beats_null` true on both the edge and the return measure.

## Corrections to MAKER.md's data section

The plan's numbers came from one 42-minute sample. Over a full week:

| | MAKER.md (42 min) | measured (8 days) |
|---|---|---|
| trades/hour, all | 2,495 | 2,713 |
| trades/hour ≥ $1 | 257 | 201 |
| trades/hour ≥ $4 | 195 | 122 |
| volume/hour | ~$37k | ~$25.5k |
| liquidity-pool share of volume | **0%** | **9.25%** |

Two of these matter. The ≥$4 rate is **37% lower** than the plan sized against, and
**9.25% of volume is liquidity-pool flow that no resting offer can capture** — the plan
records 0% and treats "all of that flow is capturable" as established. Neither changes the
verdict; both change the sizing arithmetic.

Also measured: taker direction is balanced over a week (43.5% buys) but wildly one-sided
over any single hour — a one-hour sample showed 12%. Any inventory model calibrated on an
hour of tape will be calibrated on noise.

## What the fill model does and does not know

- **Queue position is a lower bound on fills.** A quote strictly inside the touch has zero
  queue ahead of it, exactly, and needs no ladder — which is the only reason 8 days of
  pre-ladder history could be replayed at all. Everything outside the touch is charged the
  full aggregate depth on this book (measured: $8k resting within 5.5 bp), so it never
  fills. Real fills can only be more than this, not fewer.
- **Fill quality is optimistic and cannot be fixed with this data.** A one-minute book
  snapshot cannot see the sub-second reprice that precedes an informed fill. The lag table
  above is the closest thing to a measurement of it, and it is why the honest reading of
  phase 4 is "expect live fills to be worse".
- `ladder_coverage_pct` on this run is **0.1%** — the ladder and the cumulative-depth
  curve only began recording on 2026-08-17. Re-run this sweep after a week of ladder
  history; it is the one input that cannot be backfilled.

## Two bugs found by building this, both silent

1. `dex_trades._taker_side` initially derived the aggressor from Stellar's synthetic
   offer-id convention, which labelled **98% of the tape as taker-buys**. The correct rule
   is `base_is_seller` alone, and `base_is_seller` does not mean what it looks like — it
   means the *resting* side's offer sold the base asset. Settled against 36 single-hop
   trades whose direction their own Horizon operation states outright: the correct rule
   matched 36/36, the offer-id theory 2/36. Pinned in `selftest_maker.py`.
2. Horizon **ignores query parameters it does not recognise**. `assets.horizon_params(spec,
   'base')` yields `base_type=native`, but `/trades` reads `base_asset_type` — so the
   request returned the unfiltered global tape with a 200 OK, and the first symptom was a
   volume figure five orders of magnitude too large.

# Market making on the SDEX — implementation plan

Written 2026-08-16. This is FUTURE.md item 2, "Stop being a taker": every real trade
today is a `path-payment-strict-send` that crosses the spread, and against a 10–16 bp XLM
book that is the whole edge and then some. Resting offers earn that spread instead of
paying it.

FUTURE.md calls this "a strategy-class change rather than a domain change." **That framing
is wrong at the code level, and this document is the argument for why** — followed by the
build order, which is deliberately data-first because the long pole is a fill model, not
an abstraction.

---

## Verdict: a new domain, sharing the tools layer

The split is not "new domain vs. extend sdex". It is three layers, and only the top one is
a domain question:

| layer | maker vs. taker | verdict |
|---|---|---|
| `tools/stellar_trader.py` — offer lifecycle, caps | new code, one money boundary | **shared**, not domain code |
| `tools/dex_trades.py`, `tools/maker_backtest.py`, recorder | different data, different fill model | **new files**, shared dir |
| `master_agent/domain_maker.py` + `template_repo_maker/` | different genome, null, gates | **new domain** |

### Why not extend `domain_sdex.py`

**1. The genome discriminator has nowhere legal to live.** `domain.py:144-146` explicitly
forbids a per-strategy domain tag in `config.json`, "which the revision LLM rewrites and
would eventually use to reassign its own domain." Extending sdex means seven of the
largest functions in that 1578-line module — `seed_config`, `tweak_config`,
`repair_config`, `config_is_sane`, `config_signature`, `inject_experiments`,
`normalize_config` — each branch on "is this a maker config?", and the only place that
flag can sit is the file the revision model rewrites. A revision that flips it, or emits a
coherent-looking hybrid, weakens the gate for both classes at once.

`_thresholds_are_sane` is the concrete case: `buy_below < sell_above` within ±50% of spot
is a *taker* check. A maker's sanity check is on half-width, quote size and inventory band,
and its quotes are anchored to the DEX touch rather than to a remembered CEX price.

**2. One leaderboard, one hourly rank-based cull at `KEEP_TOP_N=8`.** A maker earns many
small spread captures against inventory risk; a taker in a trending hour posts a far larger
single-hour swing. Mixed into one population, the makers are culled before their edge
accumulates — precisely the cross-domain failure FUTURE.md:137-139 describes, reproduced
*inside* one domain. A separate domain means a separate process, a separate population, and
that protection for free while the class is unproven.

**3. `beats_null` diverges and the contract carries exactly one.** Buy-and-hold is not the
maker's null; the maker's null is "quote at the touch, no skew, no inventory management".
Two nulls in one domain requires the loop to know which strategy is which, which is
argument 1 again.

**4. `TEMPLATE_REPO` is a single constant, and the strategy interface changes.**
`decide(price, history, state, config) -> (side, action, requested_usd)` is a taker shape:
it answers "should I cross now?". A maker answers "where do I rest, how big, and for how
long?" — see §4.2. That is `template_repo_maker/`, exactly as `template_repo_forecast/`
was for the forecast domain.

**The cost, stated honestly:** one domain per process (`DOMAIN` env var), so a maker never
competes with a taker until FUTURE.md's deferred cross-domain leaderboard and bandit exist.
This is the same cost the forecast domain accepted, and it is the right one while the maker
class is unproven.

**One trap to resist:** sdex's genome *looks* close to a maker's — `buy_below`/`sell_above`
straddling mid is a spread. But execution semantics, fill model, caps and null all change
underneath identical-looking numbers. Similar numbers with different meanings is the shape
of the bug where `score.py`'s haircut sat at 0.899 while the prompt quoted 0.999.

---

## What the data says (measured 2026-08-16, XLM/USDC on pubnet)

These numbers were taken while writing this plan and are the reason it is ordered the way
it is.

**The tape is dense, and it is 100% order book.** Over a 42-minute sample (2000 trades) of
Horizon `/trades` for native/USDC:

| filter | trades/hour | volume/hour |
|---|---|---|
| all | ~2,495 | ~$37k |
| ≥ $1.00 | ~257 | ~$37.4k |
| ≥ $4.00 | ~195 | ~$37.2k |

`trade_type` was `orderbook` for all 2000 — **no liquidity-pool leakage**, so all of that
flow is capturable by a resting offer. The size distribution is brutally skewed (median
$0.005, p90 $0.16, max $490), so "2,495 trades/hour" is dust; the honest number is the
~195/hour at or above the size we would actually quote.

**This is the answer to criterion 5**, the one FUTURE.md says the current system fails. A
threshold taker gets a handful of independent bets per hour on one asset's price path, which
is why hourly rank is mostly sampling noise. A maker quoting at the touch sees ~195
fill opportunities per hour. That is two orders of magnitude more events per cycle, from
the same venue, with no new money boundary.

**The spread is worth having.** Recent recorder rows show `spread_bp` 9.6–16.0 and
`half_spread_bp` 4.8–8.0, against `friction.XLM_FLOOR` of 6 bp. A taker pays a half-spread
each way; a maker earns the full spread per round trip. The swing from one to the other is
~20–32 bp per round trip, against a leader that was measured turning over $5,757 for a
40.5 bp gross edge. Stellar charges no maker/taker fee — only the flat 100-stroop base fee
(~$0.0000016), which is negligible even at one quote update per tick.

**You already have half the history, and are throwing away the other half.**
`/opt/trades/.market_history.jsonl` holds 16,300 rows over 13.7 days (2026-08-03 →
2026-08-17): `dex_bid`, `dex_ask`, `spread_bp`, `bid_depth_usd`, `ask_depth_usd`, one a
minute. But `dex_price.get_orderbook` already returns the full `bids`/`asks` ladder
(`dex_price.py:151-158`) and `market_recorder.snapshot()` never references either — it keeps
top-of-book and two aggregate depth numbers and discards the levels. Keeping the top few
levels is a few lines and starts accruing maker-grade data today.

**The genuinely missing input is the trade tape.** Nothing in `tools/` touches Horizon
`/trades`. A resting offer fills when a taker crosses it, so without executed-trade
timestamps, prices, sizes and aggressor direction, any fill model is a guess. The endpoint
serves history with cursor paging, so this is **backfillable** — the backtest does not have
to wait 30 days for data to accumulate.

**Capital.** claudio currently holds 320.13 XLM (~$50) + 26.92 USDC, `subentry_count` 1
(the USDC trustline), zero open offers. At `MAX_TRADE_USD` = $4 a two-sided quote is well
within that.

**A scale reality check, so nobody misreads the outcome:** at $4 per side and a 10 bp
spread, one perfect round trip grosses **$0.008**. The live side at current caps is a
proof-of-mechanism, not an income stream. The *paper* side starts at $1000 and can quote
proportionally, which is what the loop actually ranks on — a paper maker capturing 20 round
trips an hour at $50/side is ~$1/hour, ~0.1%/hour, and that is a number worth evolving
against. Judge phase 4 on fills, uptime and sign of edge; do not judge it on dollars.

---

## Phase 0 — data first

Do this before any design work. It is small, it is outside the money boundary (no
`check_boundary_integrity` halt for `market_recorder`; see §0.1 note), and the ladder
history only accrues in wall-clock time. Even if the rest of this plan slips behind Kalshi
(FUTURE.md item 4), phase 0 should land now so the data is waiting.

### 0.1 `tools/market_recorder.py` — keep the ladder

In `snapshot()`, after the existing `book = _safe(dex_price.get_orderbook, spec) or {}`:

```python
# Top N levels per side, rounded to keep the row small. The maker fill model needs
# depth AT a price, not aggregate depth across the whole book: an offer resting at P
# fills only after the size already queued at or better than P is consumed.
_LADDER_LEVELS = 5
row['bids'] = [{'p': lv['price'], 'usd': round(lv['usd'], 2)}
               for lv in (book.get('bids') or [])[:_LADDER_LEVELS]]
row['asks'] = [{'p': lv['price'], 'usd': round(lv['usd'], 2)}
               for lv in (book.get('asks') or [])[:_LADDER_LEVELS]]
```

Row size goes from ~300 B to ~600 B; `MAX_ROWS` = 60000 still bounds the file at ~36 MB.
Bump the `_ROW_BYTES` over-estimate accordingly or `_trim()` will start rewriting the whole
file on every append (its comment explains that failure mode).

Every existing reader (`basis.latest()`, `backtest._basis_series`, `basis_report`) reads
named keys and ignores unknown ones, so this is additive. `market_recorder.py` lives in
`/opt/tools`, so **editing it does halt live trading** until committed and
`/opt/.integrity_baseline.json` is deleted — bundle it with §0.2 into one commit and one
re-baseline.

### 0.2 `tools/dex_trades.py` — new, the tape

Read-only, no money boundary logic, modelled on `ohlc_history.py`'s disk-cache shape.

```
get_trades(spec='XLM', quote='USDC', start_ts=None, end_ts=None) -> [Trade]
    Trade = {'ts', 'price', 'base_amount', 'usd', 'taker_side'}
backfill(days=30)          # cursor-paged, resumable, appends to the cache
tape_stats(hours=24)       # trades/hour and volume/hour by size bucket
```

Details that matter:

- Endpoint: `GET /trades?base_asset_type=native&counter_asset_type=credit_alphanum4&
  counter_asset_code=USDC&counter_asset_issuer=<USDC issuer>&order=asc&cursor=<paging_token>`.
  Reuse `stellar_trader._USDC_ISSUER` rather than writing the address again — "never write
  an issuer address from memory" is already the rule the revision prompt enforces.
- Price arrives as a rational `{"n": ..., "d": ...}`. Use it directly; do not round-trip
  through float division before storing.
- `base_is_seller` gives the **aggressor direction**, which is the single most important
  field for the fill model: it tells you which side of the book was consumed.
- Filter to `trade_type == 'orderbook'`. Pool trades cannot fill a resting offer.
- Cache as append-only JSONL under `/opt/trades/.dex_trades_<spec>.jsonl`, keyed by
  `paging_token` so a resumed backfill cannot double-count.
- Drop trades below a dust threshold (~$0.01) at read time, not write time — the dust is
  85% of the row count and 0.1% of the volume, but keep it on disk in case the noise turns
  out to be someone else's signal.

---

## Phase 1 — `tools/maker_backtest.py`, and the kill criterion

**Build this standalone and answer one question before writing a single line of domain or
trader code.** FUTURE.md calls the queue-position backtest "the hard part and the reason
this is #2 not #1"; if it is not tractable, this item is dead and you will have learned it
for the cost of one file.

### 1.1 The fill model

Crude, honest, and buildable from the two phase-0 datasets:

1. At minute `t`, the recorder ladder gives `Q_ahead(P)` — resting size at or better than
   your quote price `P` on your side of the book.
2. Between `t` and `t+1`, the tape gives `V(P)` — total aggressing volume that traded at a
   price that would have crossed `P`, filtered to the right aggressor direction.
3. Your fill is `min(your_size, max(0, V(P) - Q_ahead(P)))`. Queue position is modelled as
   strict FIFO behind the resting depth that was there when you joined. This is
   pessimistic — it ignores cancels ahead of you — which is the correct direction to be
   wrong in.
4. **Adverse selection is the measurement that matters.** Mark every fill against the mid
   at `t + 1, 5, 15` minutes. A maker that fills only when the price is about to keep going
   against it earns spread and loses more; a backtest that reports gross spread capture
   without this number is lying. Report `spread_captured_usd`, `adverse_selection_usd` and
   their net separately, never only the net.

### 1.2 Interface

Mirror `backtest.py`'s output contract so `domain_maker.replay()` is a thin wrapper:

```python
{'return_pct', 'null_pct', 'beats_null', 'trades', 'fill_rate',
 'spread_captured_usd', 'adverse_selection_usd', 'inventory_max_usd',
 'quote_uptime_pct', 'decide_source', 'WARNING'}
```

The null (`null_pct`) is a constant-width quoter at the touch with fixed size and no skew
and no inventory management — the honest analogue of buy-and-hold. Not "do nothing":
do-nothing is a zero-return baseline that any positive-carry strategy clears trivially, and
it would rank noise.

### 1.3 The kill criterion — write this down before running it

Proceed to phase 2 only if, over ≥7 days of tape:

- the naive constant-width quoter has **`fill_rate` > 0** and ≥ ~20 round trips/day at a
  realistic quote size (i.e. the pessimistic queue model still fills), **and**
- net of adverse selection its edge is **positive at some width** in the 5–40 bp band, **and**
- the result is not driven by a single day or a single $490-class trade — re-run per-day and
  check the sign is stable.

If spread capture is real but adverse selection eats all of it at every width, that is a
**genuine negative result** and belongs in FUTURE.md as such. Do not proceed to phase 2 to
"see if it works live". The whole reason this system has a backtest gate is that the
alternative is discovering it with money.

---

## Phase 2 — offer lifecycle in `tools/stellar_trader.py`

Only after phase 1 clears. This is the money boundary: one commit, one re-baseline, and
`MASTER_AGENT.md`'s stop procedure understood before starting.

### 2.1 The primitive exists

`stellar tx new manage-sell-offer` is available in the container's CLI:

```
--selling --buying --amount (stroops, 0 = delete) --price (n:d) --offer-id (0 = create)
```

`--offer-id` on an existing offer is an atomic cancel/replace, which is exactly the
requote primitive — no two-transaction race where the strategy is briefly unquoted or
briefly double-quoted. `manage-buy-offer` and `create-passive-sell-offer` are also present;
the passive variant is worth remembering for the case where your own two quotes would
otherwise cross.

Price must be a rational `n:d`, not a decimal. Derive it from the price the strategy asked
for with a bounded denominator, and **log the rational actually submitted**, because the
rounding is a real (if tiny) difference between the requested and resting price.

### 2.2 New functions

```python
place_offer(side, usd_amount, price, *, asset='XLM', offer_id=0) -> dict
cancel_offer(offer_id, side, *, asset='XLM') -> dict
open_offers(ours_only=True) -> [dict]       # GET /accounts/<addr>/offers
reconcile_offers(expected) -> dict          # on-chain truth vs. what we think we have
cancel_all_offers() -> dict                 # the maker's wind_down
```

`open_offers` should hit `/accounts/<addr>/offers`, verified present for claudio and
currently returning zero records.

### 2.3 Two accounting bugs this creates, and the fixes

**`_spendable_xlm` (`stellar_trader.py:296`) will double-count.** A resting sell offer does
*not* reduce `balance` on Stellar; it raises `selling_liabilities` on that balance entry.
Both `selling_liabilities` and `buying_liabilities` are present in the account JSON and are
currently `0.0000000` because there are no offers. Left as-is, the account would appear to
have XLM it has already committed to a resting offer, and `_sellable_xlm` /
`ensure_trading_cushion` / `wind_down` would all size against phantom balance. Fix
`_spendable_xlm` and `_sellable_xlm` to subtract `selling_liabilities` **before** the
existing reserve and fee-buffer subtraction. This is not optional and it is not a maker-only
concern — get it wrong and the taker paths break too, the moment any offer is open.

**Each open offer is a subentry and raises the minimum balance by the 0.5 XLM base
reserve.** `_BASE_RESERVE_XLM` already exists and `MAX_SYSTEM_TRUSTLINES` already does this
arithmetic for trustlines; `MAX_OPEN_OFFERS` must feed the same calculation, or a maker with
several quotes open silently pushes the account under its own reserve and every subsequent
operation fails.

### 2.4 New caps

Same rules as the existing block (`stellar_trader.py:63-90`): module-level, never
caller-supplied, never readable from a `config.json`, only changeable by a human edit that
`check_boundary_integrity` will halt over.

```python
MAX_OPEN_OFFERS = 4                  # subentry reserve: 0.5 XLM each, feeds min-balance math
MAX_RESTING_USD_PER_SIDE = 4.0       # matches MAX_TRADE_USD; one quote, one cap
MAX_RESTING_USD_TOTAL = 8.0          # both sides, all offers
MAX_INVENTORY_SKEW_USD = 8.0         # |long - short| before quotes go one-sided
MAX_OFFER_AGE_S = 900                # a stale quote is cancelled, not left to be picked off
MIN_QUOTE_WIDTH_BP = 2.0             # never quote inside the fee/rounding floor
```

The existing per-trade and daily caps still apply to whatever *fills*, and that is the point
of the split: a resting offer is an exposure cap question, a fill is a spend cap question,
and they are not the same limit. `MAX_OFFER_AGE_S` is a safety cap, not a strategy knob — a
maker process that dies with quotes resting is the one failure mode that loses money while
doing nothing, and it must be bounded outside the agent's reach.

### 2.5 Fill detection

Poll `/accounts/<addr>/offers` each tick and diff against expected: an offer that shrank or
vanished is a fill (or a cancel we issued — reconcile against our own ledger). Cross-check
against `/accounts/<addr>/trades`, which attributes trades to your offer IDs, and write a
`<name>.pubnet.log` line per detected fill in the existing format so `live_report.py` keeps
working. Detection is polling, not push, and it is therefore lossy at the edges — the
reconcile function is what makes it safe, not the polling.

---

## Phase 3 — `master_agent/domain_maker.py` + `template_repo_maker/`

Start from `domain_forecast.py`, not `domain_sdex.py`: forecast is the worked example of a
domain written *to* the contract rather than extracted from `monitor.py`, and copying sdex
drags in threshold-band furniture that has to be deleted anyway.

Run `python3 master_agent/domain.py maker` early and often — it arity-checks all 42 contract
members and is much faster than discovering a wrong signature at a call site that runs once
a week.

### 3.1 Contract members that differ from sdex

| member | maker behaviour |
|---|---|
| `observe()` | the **book**, not a scalar price: touch, ladder, spread, plus CEX mid for basis skew |
| `score` / `score_path` | net worth **including resting offers** — see §3.3 |
| `config_signature` | (width band, size, skew mode, refresh interval) |
| `REPLAY_DAYS` / `replay()` | wraps `maker_backtest`; `beats_null` = beats the constant-width quoter |
| `config_is_sane` | width ≥ `MIN_QUOTE_WIDTH_BP`, size > 0, inventory band sane, refresh ≥ tick — **no `buy_below < sell_above`** |
| `seed_config` / `tweak_config` | mutate width/size/skew, seeded from the recorded spread distribution the way `_seed_band_half_bp` seeds from the basis distribution |
| `caps()` | the §2.4 block |
| `retire_live` | `cancel_all_offers()` **then** `wind_down()` — order matters, see §3.4 |
| `SMOKE_ENV` | `{'PAPER_ONLY': '1'}`, same as sdex |
| `TEMPLATE_REPO` | `file:///opt/template_repo_maker` |

### 3.2 The strategy interface

`decide()` is the wrong shape and should not be kept for familiarity's sake. The maker
template's importable entry point:

```python
def quote(book, state, config):
    """Where to rest, how big. Pure and fast -- called once per backtest tick.

    Returns {'bid': (price, usd), 'ask': (price, usd)} with either side None to
    stand down on that side, or None to pull both quotes.
    """
```

The structural rules carry over verbatim from `template_repo/main.py` and must be restated
in the maker template's docstring, because they are enforced by machinery that is not
domain-specific: top level may contain only imports, assignments, defs, docstring and the
`__main__` guard (`backtest.importability_report`), no network calls inside `quote()`, and
execution — offer placement, cancel, fill accounting, logging — belongs to the shared
executor and must not be reimplemented in `main.py`. `domain_maker.importability` can reuse
`backtest.importability_report` unchanged; it is an AST walk and knows nothing about
trading.

### 3.3 Scoring must count resting offers

`score.compute_score_multi` values `balance_usd` plus marked positions. A maker with a $50
sell offer resting has that XLM committed but not sold, and a maker with a $50 buy offer
resting has USD committed. If offers are not counted, **a strategy's score drops the instant
it quotes and recovers when it cancels** — the loop would select for makers that never
quote, which is the exact shape of the 2026-08-01 bug where 45 never-traded clones occupied
every top slot.

Add `open_offers` to the maker's `state.json` shape and value each at its resting price,
subject to the same `UNREALIZED_HAIRCUT` as an XLM position. Do this in
`domain_maker.score()` rather than in `score.py`, so the sdex domain's fitness function is
not touched at all.

### 3.4 `retire_live` ordering

Cancel every offer first, *then* `wind_down()`. Reversed, `wind_down` sizes its chunks
against a spendable balance that is still encumbered by resting sell offers (see §2.3) and
either under-sells or fails outright, and any offer left resting can fill *after* the
handover — a position opened by a strategy that is no longer live, which nothing in the
system is watching. Assert zero open offers before `prepare_live` on the incoming strategy,
the same way `open_trustlines_for` is sequenced between the two today.

---

## Phase 4 — run it

New container (`./create.sh`), as FUTURE.md item 3 did for the forecast domain. This is not
optional politeness: `domain.py`'s filesystem constants are hardcoded `/opt` paths —
`STATE_FILE`, `STRATEGIES_DIR`, `TRADES_DIR`, `LIVE_STRATEGY_FILE` — and are not
domain-scoped, so `DOMAIN=maker` in the existing container would share
`/opt/strategy_state.json` and `/opt/strategies` with the running sdex population and
corrupt both.

Order: paper-only first (`PAPER_ONLY=1` in the emperor environment) for at least a day, and
check `live_report`-style fill accounting against the on-chain tape before any `live.flag`
is written. Then a single live strategy under the §2.4 caps.

Success at this stage is: quotes rest, fills are detected and reconcile against Horizon,
`quote_uptime_pct` is high, no offer outlives `MAX_OFFER_AGE_S`, and the population's
`beats_null` distribution shifts upward across cycles. **Not dollars** — see the scale note
at the end of the data section.

---

## Risks

- **Adverse selection is the whole game and the backtest models it crudely.** A 1-minute
  book snapshot cannot see the sub-second reprice that precedes an informed fill. Phase 1's
  numbers will be optimistic about fill *quality* even though the queue model is pessimistic
  about fill *quantity*, and those two errors do not cancel. Treat phase 4's live fills as
  the real measurement and expect them to be worse.
- **A dead process with resting offers loses money while doing nothing.** This is the new
  failure mode the taker design does not have — a `path-payment` either completes or does
  not. `MAX_OFFER_AGE_S`, `reconcile_offers` and a `cancel_all_offers` on shutdown are the
  three mitigations, and all three belong outside the strategy's reach.
- **The revision LLM will try to reimplement offer management inside `main.py`**, the same
  way it had to be forbidden from reimplementing `execute_trade`. The prompt must forbid it
  and `check_smoke_state` must catch it (candidate code that leaves offers open after the
  smoke test fails).
- **The tape sample is 42 minutes at one time of day.** Volume and spread are almost
  certainly diurnal. Re-measure across a full week during phase 0 before trusting the ~195
  trades/hour figure for sizing.
- **`MAX_DAILY_USD = 99999.0`** is still live from `72fc3f4 TEMP: max-daily isn't working`.
  A maker generates far more fills per day than a taker, so this cap goes from dormant to
  load-bearing. Fix it before phase 4, not after.

## Deliberately not in scope

- **Cross-venue arbitrage.** Inventory on two venues, per FUTURE.md's own note on item 1.
- **Cross-domain leaderboard, per-domain `KEEP_TOP_N`, the allocation bandit.** Still
  deferred until a second domain has actually run, which is the stance `domain.py:141-143`
  takes and this plan does not change.
- **Multi-asset making.** Extra assets were removed from the sdex domain on 2026-08-13 and
  should not come back through this door. XLM/USDC only.
- **Merging maker and taker populations.** That is the bandit work above. Until it exists,
  two containers, two domains, compared by hand.

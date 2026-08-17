# FUTURE.md — where to point this system next

**Written 2026-08-04.** Design notes, not a runbook. The goal this document serves is the
original one: a *generic* "make money" system that tries new things and reinforces what
worked. Stellar/SDEX was a starting venue, not the destination.

---

## Where things actually stand (2026-08-04)

The loop has only just started working, which changes what "next" should mean.

- The **revision LLM had been dead in every observed cycle** until 2026-08-03.
  `_run_revision` spawned a bare `python3`, and only `/opt/agents/venv/bin/python` can
  import `ollama`. Every revision died with `ModuleNotFoundError`, monitor silently fell
  back to `apply_random_tweak`, and the logs read as ordinary cycles. So the "evolution"
  on record is random threshold jitter with no model in it.
- **Nothing charged spread, fees or slippage** until the same pass. `execute_trade` and
  `backtest.py` both bought and sold at one price, so the fitness landscape was
  frictionless and the loop was selecting for churn.
- The measured consequence: the leader turned over **$5,757 for a $23.33 gain** — a 40.5bp
  edge against a real 12bp XLM book it never paid, and against 151–186bp books on the
  extra legs it kept being handed.
- The population had **141 `main.py` files with ~10 distinct hashes**, the top three
  covering 113 of them, because `apply_random_tweak` only scales thresholds.

Both root causes were fixed on 2026-08-03. **No clean read of SDEX exists yet with a
working revision model and honest friction.** Getting one is worth more than a new venue,
and switching venues before then spends the fix without learning from it.

---

## What a domain needs to plug into this loop

The reusable criterion. SDEX satisfies 1–4; almost nothing satisfies 5 today.

1. **Fitness resolvable inside a cycle.** `CYCLE_SLEEP` is 3600s and the cull is
   rank-based at `KEEP_TOP_N` (8). A domain whose feedback takes a week cannot be scored
   by this loop at all — it would be culled before its first signal arrives.
2. **A cheap offline replay.** `backtest.py` replays 720 real candles in ~1s. Without an
   analogue, `main_py_is_sane` has nothing to gate on, and an LLM's untested code reaches
   money through a gate that only checks it parses.
3. **A null strategy to measure against.** `beats_buy_hold`. Absolute return is beta, not
   skill; every domain needs its own version of this or the leaderboard ranks luck.
4. **Caps enforced outside the agent's reach.** The `stellar_trader.py:62-72` pattern:
   never caller-supplied, never overridable by a config or a revision.
5. **Enough independent bets per hour that ranking isn't noise.** *This is the one the
   current system fails.* We cull to top-8 hourly on a few hours of one asset's price
   path. Most of that ordering is sampling noise, so the loop clones the winners of coin
   flips. Domains differ enormously here, and it should drive the choice.

---

## Ranked ideas

### 1. [DONE] Mine the basis — cheapest, biggest signal-to-noise win

`tools/basis.py` was written on 2026-08-03 and **nothing calls it**. Strategies decide on
a CEX aggregate price (`price_feed.get_price()`) and execute on the Stellar DEX. That gap
is a real, mean-reverting, *structural* edge rather than a directional prediction, and it
fires many times an hour — so fitness converges within a cycle instead of a week. It is
the only item on this list that needs no new execution stack and no new money boundary.

Note this does **not** require capital on two venues: the basis is tradeable as a *signal*
on the DEX alone (buy when SDEX is cheap against the CEX mark and the spread reverts).
True cross-venue arb needs inventory on both sides and is a separate, larger project.

### 2. Stop being a taker

**Planned in detail in `MAKER.md` (2026-08-16).** That document supersedes the sketch below
on two points: it argues for a **new domain** (`domain_maker.py`) rather than the
"strategy-class change" this section assumed, and it measures the venue — ~195 trades/hour
at or above $4, 100% order book, on a 10–16 bp spread — which makes this the item that
actually fixes criterion 5.

Every real trade is a `path-payment-strict-send` — a market-order-like take that crosses
the spread on every fill. Against the 12bp XLM book and 151–186bp extra-leg books, that
is the whole edge and then some. Resting offers (`manage_sell_offer`) are a *different
business*, earning the spread instead of paying it, and the SDEX is one of the few venues
where an individual can actually run one.

This is a strategy-class change rather than a domain change, and it may be where the money
was the whole time. It needs: order lifecycle in `stellar_trader.py` (place / cancel /
detect fill), inventory-aware sizing, and a `backtest.py` that models queue position at
least crudely — the last is the hard part and the reason this is #2 not #1.

### 3. Prediction markets (Kalshi or Polymarket) — the real second domain

Best fit of anything genuinely different from price trading:

- **Ground truth on resolution**, and a proper backtest already exists in the form of
  calibration / Brier score over *already-resolved historical markets*.
- **The null baseline is the market's implied probability** — a clean, honest analogue of
  `beats_buy_hold`.
- **Hundreds of uncorrelated markets**, which is the structural fix for criterion 5.
- Most importantly, it **moves evolution from threshold tweaking to research and
  reasoning**. The ~10-distinct-hashes degeneracy is what happens when the only mutation
  operator scales numbers. On a domain where the edge is reading and estimating, the LLM
  becomes the source of variance instead of a rounding error.

Caveats: many markets resolve in days, so score on mark-to-market plus running calibration,
not resolution alone. Kalshi is CFTC-regulated with a real REST API, which matters for the
money boundary; Polymarket is on Polygon and brings a wallet/chain stack of its own.

### 4. Shorting

[DONE (for now)].  There is a manual shorting mechanism already in place.
See `.short_buffer.json`.

### 5. Non-market income — the end state, not the next step

Services, content, arbitrage of our own compute. This is the honest destination for
"generic make money," but the feedback clock is days-to-weeks against a 1h cycle, and it
introduces counterparties and terms-of-service surface that no current gate models. It
needs a *different loop*, not a different tool. Revisit only after the domain-plugin work
below exists and after at least one non-trading domain has run successfully.

---

## The architectural change that makes it generic

The domain is currently welded in at three places:

- `master_agent/score.py` — fitness assumes a portfolio net worth
- `tools/backtest.py` — replay assumes OHLC candles
- `tools/trade_logger.py` / `tools/stellar_trader.py` — execution assumes a DEX swap

**Make the domain a plugin.** A strategy declares its domain; monitor calls
`domain.score(state)`, `domain.replay(main_py)`, `domain.execute(...)`, `domain.caps`.

Do this **as part of adding domain #2, not speculatively before it.** Abstractions built
against one example are built wrong.

Then the piece currently missing for the stated goal:

- **One leaderboard across domains**, with fitness normalized as *excess over that
  domain's null, in risk units* (Sharpe-like, not raw %), so a prediction-market strategy
  and an SDEX strategy are comparable at all.
- **`KEEP_TOP_N` and the live slot become per-domain**, with a bandit allocating clone
  budget across domains by realized excess return.

That second bullet is the actual mechanism for "try new things and reinforce what worked"
*at the domain level*. Without it, whichever domain happens to be working this week wins
the entire population and the other is culled before it learns anything — the same
failure mode `TEMPLATE_SPAWNS_PER_CYCLE` exists to prevent within a single domain.

---

## Recommended order

1. [DONE] **Basis (#1) first.** Half the code exists, it fixes the noise problem, and it stays
   inside the venue we understand.
2. [DONE 2026-08-05] Factor out the
   "domain" logic.  Analyze what is in the "What a domain needs"
   section, and implement it such that the current Stellar SDEX code functions
   as it does now.

   `master_agent/domain.py` is the contract (its docstring is the spec, written for the
   next emperor pass), `domain_sdex.py` is this venue, `domain_null.py` is a no-money
   reference domain, `selftest_domain.py` is a differential test against the pre-refactor
   `monitor.py` in git — 414 checks, all passing. monitor.py went 2819 → 1313 lines.

3. [DONE] Create a new benchmark domain domain with free, fast, unambiguous scoring
   and no money at all - a forecasting benchmark, say - for a day. Ensure that
   the population improves.  This will be done in a fresh new container.

4. **Prediction markets as a new domain**.  Use the kalshi public API to get
   real prediction markets.  See "#3. Prediction markets (Kalshi or Polymarket)" above.

5. Market making (#2 "Stop Being A Taker" above) whenever the queue-position backtest looks
   tractable. **See `MAKER.md`** for the implementation plan, including the fill model that
   settles "tractable" and the kill criterion for abandoning the item.

   Its phase 0 (record the order-book ladder in `market_recorder.py`; add
   `tools/dex_trades.py` to backfill the Horizon trade tape) is cheap, sits outside the
   money boundary, and only accrues in wall-clock time — worth landing *before* item 4 even
   though the rest of the item comes after it.


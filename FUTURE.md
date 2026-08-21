# FUTURE.md — where to point this system next

**Written 2026-08-04.** Design notes, not a runbook. The goal this document serves is the
original one: a *generic* "make money" system that tries new things and reinforces what
worked. Stellar/SDEX was a starting venue, not the destination.

---

## The nine criteria — score any candidate domain on these first

**1–5 written 2026-08-04; 6–9 added 2026-08-21 after the Kalshi post-mortem.** This list is
the rubric, and it belongs at the top because it is what a new domain proposal should be
argued against before anything else in this document is read.

Criteria 1–5 are elaborated in "What a domain needs to plug into this loop" below, and are
summarized here only so the whole rubric reads as one list:

1. **Fitness resolvable inside a cycle.** Hourly rank-based cull; week-long feedback cannot
   be scored at all.
2. **A cheap offline replay.** Without one the gates only check that the code parses.
3. **A null strategy to measure against.** Absolute return is beta, not skill.
4. **Caps enforced outside the agent's reach.** Never caller-supplied, never config-readable.
5. **Enough independent bets per hour that ranking isn't noise.** Judged when *choosing* a
   domain — deliberately not a `domain.py` member.

**Criteria 1–5 are necessary but not sufficient.** `domain_kalshi.py` satisfied 1–4 and
arguably 5, ran cleanly for a week, and produced no edge and no profit. The machinery was
never what failed. What was missing was a test for whether the domain could pay *anyone*,
which is what 6–9 supply. See `KALSHI.md`'s OUTCOME section for the run these are drawn
from.

6. **Where does the information come from?** `KALSHI.md` §3 is the root cause of that
   failure and it generalizes: if the genome's inputs are a subset of what the price
   already reflects, no rule over them has positive expected value. Evolution can select
   noise from such a population but it cannot manufacture information. A domain survives
   this exactly two ways — the edge is **structural** (you are paid for providing a
   service: spread, liquidity, funding, settlement risk) so no information is needed at
   all, or the genome ingests **exogenous** data the price has not absorbed. "The LLM will
   think harder about the same price series" is not a third way.
7. **Is the score net-of-friction P&L?** `KALSHI.md` §4: `score_path()` ranked on shrunk
   Brier edge, which is a proper scoring rule, correctly implemented, and *not money*. The
   two came apart in practice — a positive Brier edge and a negative P&L at the same time.
   Any proxy metric will eventually be optimized away from the goal. This also has to make
   "do not trade" a representable action, which a pure forecast-accuracy score cannot.
8. **Is the prize big enough at capacity?** `KALSHI.md` §5: median market volume 647
   contracts, so even a fat 5% net edge is tens of dollars. Ask before building, not after.
   Note the honest version of this test for *this* system: at `MAX_TRADE_USD` $4 and
   `MAX_TOTAL_NONBASE_EXPOSURE_USD` $8, no domain here earns meaningfully, so the real
   question a candidate must answer is **what running it teaches** that the last run
   didn't. Two paper domains have already returned "the loop selects noise"; a third that
   returns the same is not worth a week.
9. **Can the mutation operator produce real variance?** The population once held 141
   `main.py` files with ~10 distinct hashes because `apply_random_tweak` only scales
   thresholds. If a candidate's genome is also a bag of numbers, it degenerates the same
   way regardless of how good the venue is. The related failure to watch for is a domain
   where the null strategy *is* the optimal strategy (passively supplying a lending pool,
   say) — there the population converges in one cycle and there is nothing left to evolve.
   Prefer domains where the genome is `main.py` itself, so `_run_revision` rather than
   `tweak_config` is the operator that matters.

**The pattern 6–9 exposes, stated once:** every domain in this document monetizes capital,
and capital is the scarce resource — the account holds tens of dollars while the container
runs a frontier model 24/7. That asymmetry is why each honest analysis here terminates in
"the prize was never large," and it is a reason to weigh domains whose product is the
model's output rather than a return on the balance.

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
These are criteria 1–5 of the nine at the top of this document — necessary, but not
sufficient on their own. Criteria 6–9 are stated there and only there.

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
5. **Enough independent bets per hour that ranking isn't noise.**
   We cull to top-8 hourly on a few hours of one asset's price
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

**Extending it past XLM/USDC (2026-08-21).** Not a new domain — a wider inventory for the
same one — but the constraint is account structure, not strategy. `MAX_OPEN_OFFERS = 4`
(`stellar_trader.py:120`) is exactly two pairs quoted two-sided; `MAX_SYSTEM_TRUSTLINES = 3`
caps which assets can be held at all; and `MAX_RESTING_USD_TOTAL = 8.0` spans every offer,
so a second pair halves the first one's quote to $2 a side rather than adding budget. All
raisable, none free: an offer is a subentry costing the same 0.5 XLM base reserve as a
trustline, and `MIN_TRUSTLINE_RESERVE_XLM` already folds `MAX_OPEN_OFFERS` in for the reason
line 163 gives — a maker that pushes the account under its own minimum can neither place the
offer, nor unwind the position, nor pay the fee to cancel.

So the case for more pairs is **statistics, not income**: with a fixed resting budget,
expected profit is roughly flat and the number of independent fill streams multiplies, which
is criterion 5 and the thing this system is actually short of. Measure before building —
spread versus flow on the candidates, a day of the recorder and `dex_trades.py`. Thinner
pairs quote wider, which looks like more edge per fill and usually is not: wide spread
generally means sparse flow and worse adverse selection, so you get filled mostly when you
are wrong. XLM/USDC at 10–16bp against ~195 fills/hour may already be the venue's best pair.
Worth checking at the same time whether any SDEX liquidity-incentive programme pays
emissions for quoting a candidate pair — that would be income for behaviour the maker
already performs, and the one place the small capital base is not the binding constraint.

### 3. [CLOSED 2026-08-18] Prediction markets (Kalshi or Polymarket)

**Built, run for a week, and closed. Not a viable domain for profit — see `KALSHI.md`'s
OUTCOME section, which is also where criteria 6–9 at the top of this document come from.**
The machinery carried the domain without special-casing; the market had no edge for a
price-only genome to take. Polymarket is dead for the same reason and is listed under
"considered and rejected" below. The optimistic case as written on 2026-08-04 is kept
verbatim below, because the gap between it and the outcome is the whole argument for
criterion 6.

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

### 6. Sell the model's output, not the balance — benchmark and competition leaderboards

**Added 2026-08-21.** The first candidate that answers criterion 8 by refusing its premise.
Every other item in this document converts capital into more capital, and the account holds
tens of dollars; the abundant resource is a frontier model running 24/7 in the container.
This is the domain shape where that asymmetry is the product: Kaggle-style competitions,
public benchmark leaderboards, anything where an artifact is submitted and scored
automatically.

The fit to `domain.py` is unusually literal:

- `replay()` is a **held-out validation split**. Criterion 2 asks for a cheap offline
  replay and this domain supplies one natively — no recorder, no `market_recorder.py`
  analogue, no history to backfill. It is the only candidate where criterion 2 costs
  nothing to satisfy.
- `beats_null` is the competition's published baseline — a cleaner null than
  `beats_buy_hold` ever was, and one nobody can argue with.
- Criterion 1 resolves in **seconds**: fit, score locally, done.
- Criterion 5 stops binding. Evaluation is local and unmetered, so the loop gets hundreds
  of independent bets an hour rather than the ~195 fills `MAKER.md` measured or the handful
  of price paths sdex offers. Nothing else in this document is within two orders of
  magnitude.
- **Criterion 6 does not apply.** `KALSHI.md` §3 says a genome whose inputs are a subset of
  what the price reflects cannot have positive EV. There is no price here and no market
  that already absorbed the inputs — the model produces an artifact that did not previously
  exist. The loop stops searching for an edge and starts manufacturing one.
- Criterion 9 is satisfied by construction. The genome **is** `main.py` — real model code,
  not a config of thresholds wrapped in it. `tweak_config` becomes nearly irrelevant and
  `_run_revision` becomes the only mutation operator that matters, which is exactly what
  item 3 hoped for and did not get.
- `caps()` returns `None` and `SMOKE_ENV` is `{}` — **no money boundary at all**. What
  replaces it is a *reputation and rate-limit* boundary: submission quotas, and the cost of
  being banned. That is a kind of cap this codebase has never modeled, and it needs
  designing rather than assuming. It is still group 4's job, and it still fails closed.

Weaknesses, stated honestly: prize money resolves in months even though the score resolves
in seconds — fine for the loop, which only consumes the score, but it means no revenue
signal for a long time and criterion 7 is satisfied only in the sense that the score *is*
the objective rather than a proxy for it. The competition is strong humans running their
own models. And automated-submission terms vary per venue and must be read, not assumed —
that reading is the phase-0 spike, and a "no" there kills the item cheaply.

### 7. Code bounties scored by CI — the same insight, weaker scoring loop

**Added 2026-08-21.** Open-source bounty boards where a PR that passes tests gets paid.
`replay()` is the repository's own test suite; `beats_null` is "the suite still passes";
feedback is minutes; the payment is real money rather than a proxy. It clears criterion 6
the same way item 6 does, and for the same reason.

Ranked below item 6 on two counts: a **human merge sits between the score and the money**,
so the fast signal and the paying signal are different signals — and spamming maintainers
carries a reputational cost that has to be an explicit gate rather than an afterthought.
Worth revisiting if item 6's phase-0 finds the submission terms hostile.

### 8. On-chain flow — the one *forecasting* domain that survives criterion 6

**Added 2026-08-21.** Every forecasting idea in this document dies on criterion 6 unless the
genome sees something the price does not already contain. Stellar's ledger is fully public,
streams from Horizon, and costs nothing: large payments into and out of known exchange
accounts, issuer mints and burns, trustline creation waves, anchor redemptions. None of that
is in `price_feed.get_price()`'s CEX aggregate, and it leads price by minutes.

`domain_flow.py`, genome = which event classes, what magnitude threshold, what lag, what
holding period. Feedback resolves in minutes, so criterion 1 is comfortable. Stellar-native,
so it reuses the existing money boundary and needs no new chain stack.

**Criterion 5 is the open question and it is cheap to answer**: measure the actual event
rate off Horizon for a day, with a `dex_trades.py`-shaped script, before writing any domain
code. `KALSHI.md`'s "what would have to be true to revisit this" ranks exogenous
information as the only route that creates a forecasting edge *and* the one most likely
already occupied — but that judgement was about NWS data behind a 1.0¢ professional quote.
Stellar on-chain flow at $4 a trade is a much less crowded corner.

### 9. Funding-rate harvest on a perp DEX — right shape, wrong venue for us

**Added 2026-08-21.** Structurally the best-fitting trade considered: perpetual futures pay
funding on a fixed clock, so you are paid continuously for absorbing an imbalance rather
than for predicting anything (criterion 6, structural branch). Short the perp against long
spot when funding is positive, delta ≈ 0, collect every hour. Unlike item 3, **the null
strategy is itself profitable** — "equal-weight everything with positive funding and hold"
makes money in a normal market — so the population manages risk on top of a positive carry
instead of hunting an edge from zero. Criterion 5 is answered by breadth: 100+ markets, each
with its own rate.

It is ranked last of the live items for three reasons, in increasing order of severity:

1. **The trade is slow even though the payment is fast.** At an ordinary 0.01%/hr, a round
   trip costing 10–20bp all-in across two legs needs tens of hours of funding just to break
   even. The loop would punish a position for its entry cost in hour one and only reward it
   around hour twenty. Fixable in `score_path` by scoring cumulative
   `funding_accrued − friction_paid` over a position's life rather than an hourly delta,
   but it has to be designed, not discovered — the same collision `domain_kalshi.py`'s
   `RANK_GRACE_S` comment documents.
2. **Hourly basis noise swamps the hourly signal.** Delta-neutral P&L per hour is funding
   plus the change in basis; funding is ~1bp and the basis wanders 5–20bp. It mean-reverts
   over longer horizons and diversifies across markets, but the honest scoring horizon is
   days against an hourly cull.
3. **There is no perp market on Stellar worth trading, and the caps make the live version
   negative anyway.** `MAX_TOTAL_NONBASE_EXPOSURE_USD` is $8; at 1bp/hr that is under two
   cents a day, less than one transaction's gas on any EVM-adjacent chain. Doing it for real
   means a different chain, a different wallet, and rebuilding every safety property in
   `stellar_trader.py`. Paper-only is possible and cheap — but see criterion 8: two paper
   domains have already returned "the loop selects noise," and this would be the third.

### Considered and rejected (2026-08-21)

Recorded so they are not re-proposed. `KALSHI.md` set the precedent that a negative result
is worth keeping.

- **Lending carry on Blend (or any Soroban lending pool).** Structurally the same trade as
  item 9 and Stellar-native, which is why it looked attractive. It fails **criterion 9**
  decisively: with a handful of pools and rates that move on a scale of days, passive supply
  has the null strategy and the optimal strategy in the same place, and the population
  converges in one cycle with nothing left to evolve. Liquidations fail criterion 5 (a
  handful of events a week, contested by bots reading the same public state). Looping does
  create a real strategy space — leverage ratio, health buffer, unwind triggers — but it
  means handing the agent a borrow facility, and `SHORTING_PLAN.md` deliberately chose
  pre-funded inventory over "Blend or any other lending protocol" for exactly that reason;
  reversing that needs an argument, not a shrug. Blend's realistic leverage (2–3x before the
  health factor bites) is not enough to change the arithmetic either. **Its honest use here
  is treasury, not evolution**: idle quote inventory can sit in a pool between trades, which
  is a supply/withdraw helper and a cap, not a `domain_blend.py`.
- **AMM-versus-orderbook atomic cycles on Stellar.** `path-payment-strict-send` routes
  across both pools and the book in one transaction, so a profitable cycle is riskless —
  no leg risk, no inventory, and it either profits or reverts. Maximally structural. But the
  genome is thin (which cycles, minimum profit, sizing, fee bid), which is criterion 9's
  bag-of-numbers failure, and opportunities are bursty and contested. **A good tool, a poor
  domain** — worth building as a free-money floor the maker sweeps alongside its quotes,
  not as a population with its own leaderboard.
- **Cross-sectional stat arb** (rank N assets, long/short the deciles). Fixes criterion 5 by
  breadth, but the genome is a function of price history alone, so criterion 6 objects
  exactly as it did to item 3. Also needs a shorting venue we do not have.
- **Polymarket**, the other half of item 3. Friction is lower than Kalshi's — no
  per-contract fee — but `KALSHI.md` §3 is the root cause and §§1–2 are consequences. A
  price-only genome against a well-made market has no edge to harvest regardless of the fee
  schedule.

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

4. [DONE, CLOSED 2026-08-18] **Prediction markets as a new domain**.  Use the kalshi
   public API to get real prediction markets.  See "#3" above and `KALSHI.md`'s OUTCOME.
   Built and run; no edge and no profit. Step 7 of its own plan was correctly never reached.

5. Market making (#2 "Stop Being A Taker" above) whenever the queue-position backtest looks
   tractable. **See `MAKER.md`** for the implementation plan, including the fill model that
   settles "tractable" and the kill criterion for abandoning the item.

   Its phase 0 (record the order-book ladder in `market_recorder.py`; add
   `tools/dex_trades.py` to backfill the Horizon trade tape) is cheap, sits outside the
   money boundary, and only accrues in wall-clock time — worth landing *before* item 4 even
   though the rest of the item comes after it.

6. **Unsequenced (added 2026-08-21): ranked items 6–9.** Deliberately not given positions
   here, because each is gated on a cheap measurement that could kill it, and sequencing
   before those run would be guessing:

   - item 6 (competition leaderboards) — read the automated-submission terms of two or
     three candidate venues. A "no" ends the item for the cost of an afternoon.
   - item 8 (on-chain flow) — a day of Horizon event-rate recording answers criterion 5.
   - item 2's pair extension — a day of the recorder on the candidate pairs, which is the
     same phase-0 work item 5 above already wants and therefore nearly free.

   Item 6 is the one to run first on merit: it is the only candidate that answers criterion
   8 rather than conceding it. Item 9 should not start until at least one of the above has
   produced a result, per criterion 8's note about third paper domains.

# YIELD.md — yield rotation across Stellar's lending and reward venues

**Written 2026-08-21. Amended 2026-08-21. CLOSED 2026-08-24** — see the OUTCOME
section below, which supersedes everything after it. The amendments were made after the
open questions at the bottom were answered; they are marked inline, and every one of them
narrows the case rather than widens it.

A candidate domain: allocate capital across Aquarius reward pools
and Blend's two main lending pools (YieldBlox and Fixed), rotating as their rates move.
This document is the case for investigating it, the two measurements that decide it, and
the three ways it could quietly fail. It is *not* an implementation plan — `MAKER.md`'s
phase structure would be premature here, because the cheap test below can end the item.

FUTURE.md's ranked item 9 is the one-line version. This is the argument.

---

## OUTCOME — closed 2026-08-24. Killed by its own measurement 2.

**Everything below this section is the plan as written on 2026-08-21, kept as history.**
The domain was built, deployed to `ssr_agent01` as `DOMAIN=yield`, and run 2026-08-21 →
2026-08-24. Measurement 2 — the kill criterion this document set — returns **zero**. Do
not spend further time on rotation across Blend in the expectation that an edge is in
there; there is not one, and the reason is structural rather than a tuning problem.

**The machinery is not what failed**, again. The contract carried a fifth domain with no
special-casing in `monitor.py`, `score_path` recomputed returns honestly from evidence,
and the leaderboard reported the true answer on its first cycle. This is a negative result
about the *venue*, and — see §5 — a positive result about how cheaply this system can now
kill a bad domain, reached expensively.

### 1. The kill criterion, answered: the ceiling is zero

Measurement 2 asks what a perfect-foresight rotator earns against the best static
allocation. Computed over the recorder's full history — 790 samples, 2026-08-21 17:52 →
2026-08-24 11:44, 65.7h — net of `yield_replay.py`'s cost model, at a range of book sizes
with the eligibility floor set equal to the book:

| book = floor | null (static best) | optimal rotation | headroom | oracle rotations |
|---|---|---|---|---|
| $60 | 8.05% | 8.05% | **0.00 pp** | 0 |
| $250 | 6.54% | 6.57% | 0.03 pp | 0 |
| $1,000 | 6.54% | 6.57% | 0.02 pp | 0 |
| $5,000 | 6.54% | 6.54% | 0.00 pp | 0 |
| $25,000 | 6.54% | 6.54% | 0.00 pp | 0 |
| $100,000 | 6.53% | 6.53% | 0.00 pp | 0 |

An oracle holding the entire future rate history **declines to rotate at any size.** This
document's test — "if it beats sitting still by one percent of APY, the domain is dead
before it starts" — is not narrowly failed, it is failed at zero. There is no rotation
edge, so no strategy, genome or revision model can find one.

### 2. Measurement 1: the ranking does flip, and the flips are worth less than the trade

Flip counts over the same 790 samples, by eligibility floor:

- **$60** — zero flips. YieldBlox/PYUSD leads in 790 of 790 samples.
- **$1,000 and $10,000** — one flip. YieldBlox/USDC leads 404 samples, Fixed/USDC 386.
- **$100,000** — two flips. Fixed/USDC 700, YieldBlox/USDC 90.

So the ordering is not frozen at realistic size, and measurement 1 taken alone looks
survivable. It is not, because the two contenders are the same asset in two pools whose
*diluted* rates sit within a few basis points of each other. §1's headroom is the entire
value of acting on every flip with perfect foresight: **2–3 bp of annualized APY**. A flip
persists roughly 32h, so capturing one is worth on the order of 0.01 bp of NAV, against
`SAME_ASSET_BP = 1.0` — one basis point of NAV, paid outright, per rotation. The crossing
costs about two orders of magnitude more than the flip is worth. That is why the oracle
sits still.

This is the trap in measurement 1 as this document wrote it. "How often does the ranking
flip, and by how much?" treats the two clauses as equally weighted; the second one carries
the entire result, and the first can pass while the domain is dead. **Measurement 2 is the
only one that matters, and it is the one that was run last.**

### 3. The leaderboard was correct, and it read as broken

At shutdown: 51 registered strategies, 45 with enough covered history to score, 6 too young.
Eighteen of the top thirty sat at exactly **1000.00**.

That is `STARTING_SCORE` plus an annualized excess of zero over the null — i.e. tied the
benchmark, which §1 establishes is the ceiling. The score looking stuck was the domain
reporting, correctly and on its first cycle, that the population had found the optimum and
there was nothing above it. Scores *below* 1000 are strategies that failed to sit still;
`seed_dcdbb185a679` earned 2.4 bp against the null's 4.2 bp over 46.1h, which annualizes to
−342 bp and a score of 657.

The population held **46 distinct genomes across 51 strategies** — variance was not the
problem — and all of them converged on the same score, because all five knobs are inert
here:

- `min_edge_bp` never binds; the ordering effectively never changes.
- `max_venues > 1` is strictly punished. The equal-weight null returns **−2.3% to −3.3%**
  annualized depending on book size: breadth pays turnover for nothing.
- `min_free_liquidity_usd` is worse than inert, it is *inverted* — see §4.
- `rebalance_hours` gates a rotation that should never happen.
- `emission_weight` multiplied **zero for the entire run**: across all 15,840 venue-samples
  in the window, not one venue reported a non-zero `emission_apr`. The knob this document
  called "section 1's asymmetric friction made searchable" could not be searched at all.

  **Unresolved:** `domain_yield.py`'s own docstring records Etherfuse paying "1.67% + 2.98%
  of BLND emissions" on 2026-08-21, the day the recorder started. Either Blend emissions
  genuinely went to zero across the whole reward zone that day, or `yield_venues.py` stopped
  populating `emission_apr`. Worth ten minutes before any of this code is reused, and it
  does not affect §1 either way — emissions would have to be large *and* volatile to create
  rotation headroom, and the base-rate result says the venues do not diverge.

### 4. A benchmark bug hid the shape of it

`yield_replay.py:392`:

```python
def _eligible(venues, floor=MIN_FREE_LIQUIDITY_USD):
```

Default arguments bind at import. `MIN_FREE_LIQUIDITY_USD = BOOK_USD` is evaluated once, so
**raising `BOOK_USD` does not raise the eligibility floor** — every call site that omits
`floor` keeps judging at $60 forever, and both nulls plus `optimal_rotation` omit it.
Raising the book makes shallow venues worse through dilution, exactly as the `BOOK_USD`
comment promises, but never excludes them, exactly as the `MIN_FREE_LIQUIDITY_USD` comment
promises it will.

```python
def _eligible(venues, floor=None):
    floor = MIN_FREE_LIQUIDITY_USD if floor is None else floor
```

Consequence for this run: the entire live population was ranked against **YieldBlox/PYUSD,
a pool holding $220–221 of free liquidity, constant across all 790 samples** — 3.7 books at
the $60 floor. Any strategy whose genome drew a sane real-money `min_free_liquidity_usd`
was structurally barred from the only venue that could tie the null, and could therefore
only score below 1000. The loop spent three days selecting *against* the one trait you would
want before touching real money.

Note what this is an instance of. The comment above `MIN_FREE_LIQUIDITY_USD` explains that
the arbitrary $1000 it replaced "came to be enforced against the benchmark and not against
the strategies the benchmark judges." The replacement reintroduced an inconsistency of the
same shape, by a different mechanism, in the same expression that fixed it. It did not
change the verdict — §1's table applies the floor correctly and still returns zero — but it
is why the run's own leaderboard was measuring a dust pool.

### 5. Criterion 9 called this in advance, and the process failure is the reusable part

FUTURE.md's criterion 9 names this exact failure by name: *"a domain where the null strategy
is the optimal strategy (passively supplying a lending pool, say) — there the population
converges in one cycle and there is nothing left to evolve."*

This document's opening section answers that objection directly: cross-venue allocation is a
fourth design, and "null equals optimal" was an argument about *one* pool, defeated by
venues "whose rates are driven by independent mechanisms — Aquarius by voted emissions,
Blend by utilization."

**That rebuttal did not survive its own amendment.** The Aquarius half was retired the same
day — "one moving, one nearly static" — which leaves Blend alone, and Blend alone is the
single-pool case criterion 9 named. The amendment even states the consequence ("a direct hit
on measurement 1: a ranking flip needs both sides to move"). The premise for the whole domain
was withdrawn in writing before implementation began, and implementation began anyway.

The cost of not stopping there:

| | lines |
|---|---|
| `master_agent/domain_yield.py` | 1,069 |
| `tools/yield_venues.py` | 737 |
| `tools/yield_replay.py` | 559 |
| `template_repo_yield/main.py` | 478 |
| `tools/yield_recorder.py` | 269 |
| `tools/yield_backtest.py` | 258 |
| **total** | **3,370** |

plus a container bootstrap and a three-day run of 51 strategies against a frontier model.
This document said: *"Get the number before writing a line of `domain_yield.py`."* The
number was obtained after all 3,370 of them, and once the recorder had data it took
minutes to compute. `MAKER.md` got this ordering right — phase 0 records, phase 1 answers
a pre-registered kill criterion, and only then does a domain module exist. This document
declared the same discipline and did not follow it.

**The lesson is not "measure first" — that was already written down.** It is that a
kill criterion has to be attached to something that *blocks*, because a plan cannot enforce
itself. The recorder needed to run before the measurement either way; the mistake was
building the domain during the wait rather than only the measurement.

### The honest caveat

This is 2.7 days of live recorder, not the months of backfill this document asked for.
That is the one real reservation and it should be stated plainly.

The mechanism argues the answer will not change. Blend supply APY is a mechanical function
of utilization; a deep pool's utilization moves slowly; and the two contenders at realistic
size are USDC in two pools that track each other. §1's result is not a small-sample
artifact — an oracle declining to rotate at *every* size tested is a statement that the
gaps are narrower than the crossing cost, and gaps that narrow do not widen because you
observed them longer.

What a longer history could show is a **stressed regime**: a utilization spike driving one
pool's supply rate far above another's, for long enough to pay the crossing. Nothing like
that occurred in the window. That is the only thing that could reopen the item, and §"What
would have to be true" says how to check it without rebuilding anything.

### What would have to be true to revisit this

1. **A stressed regime, verified offline.** Replay archived pool events (the Hubble/BigQuery
   `crypto-stellar` or Galexie route already flagged above — pick the source before promising
   the backfill) and re-run §1's table across a period containing a real utilization shock.
   If the oracle still declines to rotate, the item is closed permanently. This needs no
   capital, no domain module and no population, and it is the *only* measurement worth
   spending time on here.
2. **Not Aquarius**, per the amendment above. A mechanism being replaced cannot be
   backtested, and a perfect-foresight number computed against the outgoing voting system
   measures a game no longer on offer.
3. **Not by making the domain smarter.** Every knob in the genome operates on a decision the
   oracle says should never be made. Revision, tweaking, a better `main.py` and a longer
   `RANK_GRACE_S` all search a space whose maximum is zero.

### What the run left behind

Stopped 2026-08-24: 51 strategy processes wound down and `emperor` stopped via supervisor at
11:37 (verified — zero `strategies/*/main.py` processes remain, `supervisorctl` reports
`emperor STOPPED`). Nothing was promoted and nothing could have been: `live_enabled()` and
`can_execute_live()` are constant `False` in this domain, so no real money was ever at risk.

The code is kept rather than deleted. `domain_yield.py`, `template_repo_yield/` and
`tools/yield_*.py` are the record that the domain contract carried a fifth domain with none
of sdex's furniture and no special-casing in `monitor.py` — the same reusable positive
`KALSHI.md` recorded. `yield_recorder.py`'s accumulated history is the input to revisit-test
1 and should not be discarded.

---


## First: correcting the rejection

FUTURE.md's "considered and rejected" list carried a Blend entry on 2026-08-21 that has
been removed, and the reasoning is worth keeping because the correction is instructive.

That rejection evaluated three designs and dismissed all three:

- **passive supply into one pool** — the null strategy and the optimal strategy sit in the
  same place, so the population converges in one cycle and there is nothing left to evolve
  (criterion 9);
- **liquidations** — a handful of contested events a week (criterion 5);
- **looping** — a real strategy space, but it hands the agent a borrow facility, and
  `SHORTING_PLAN.md:7` deliberately chose pre-funded inventory over "Blend or any other
  lending protocol" for exactly that reason.

**Cross-venue allocation is a fourth design, and two of those three objections do not
survive it.** "Null equals optimal" was an argument about *one* pool; across venues whose
rates are driven by independent mechanisms — Aquarius by voted emissions, Blend by
utilization — there is a real allocation problem with no closed-form answer. And pure
supply-side rotation never borrows, so the `SHORTING_PLAN.md` conflict does not arise.

What survives is the criterion 5 concern, and the horizon question below cuts both ways
on it.

---

## Why an edge could exist here at all

The edge is **not** harvesting yield. The null captures that, anyone can do it, and a
domain whose strategies merely collect the base rate is measuring beta. The edge is
*anticipating rate changes*, and the reason that is not a criterion 6 violation —
`KALSHI.md` §3, "no rule over inputs the price already reflects can have positive EV" — is
the important part:

**An APY is not a price.** Aquarius emissions are set by on-chain voting, with vote
accumulation visible and period boundaries known in advance. Blend supply APY is a
mechanical function of pool utilization, which moves the moment someone borrows or repays,
visibly, on chain. In both cases the information exists *before* the rate reflects it, and
the rate does not move in anticipation the way a market price would — because it is not set
by anyone's expectations. It is a deterministic output of a state variable.

And the capital that would arbitrage that lag is slow. Nobody rotates a large position for
eighty basis points of APY on a weekly voting cycle. **That stickiness is the structural
reason a rotation edge could persist**, and it puts this domain on criterion 6's exogenous
branch rather than its structural one.

This is the same insight as FUTURE.md's item 8 (on-chain flow), applied to yields instead
of prices — and it is stronger, because the quantity being predicted is mechanically
determined rather than being a price that other participants are also forecasting.

**Amendment: the Aquarius half of this argument is being retired.** Aquarius's voting system is
changing, and its reward mechanics will be much less dynamic in future. A rate that stops
moving cannot be anticipated, so the problem on that side is not that the lag is too small to
arbitrage — it is that there is progressively less to arbitrage. What survives intact is Blend,
whose supply APY remains a mechanical function of utilization and moves the moment anyone
borrows or repays. The premise weakens from *two independent rate mechanisms, both moving* to
*one moving, one nearly static*, which is a direct hit on measurement 1: a ranking flip needs
both sides to move.

---

## The two measurements that decide it

The reason to take this seriously ahead of the other candidates: **pool state is on chain
and historical, so it is backfillable.** Unlike the maker, which needed weeks of
`market_recorder.py` before any backtest could exist, months of APY, emission and
utilization history should be reconstructible immediately. Criterion 2 comes nearly free.

**Amendment: criterion 2 is not free, for two unrelated reasons.** Public Soroban RPC retains
about seven days — asked for events at ledger 1000 on 2026-08-21, mainnet replied that the
available range was 63,940,515–64,061,474, roughly 121k ledgers at ~5s each. Contract state
reads are current-value only; there is no *state as of last March* call. So months of history
means replaying pool events out of an archive — Hubble/BigQuery's `crypto-stellar`, the Galexie
datalake, or raw history archives — which is real work with its own gaps rather than an
immediate read. **Pick the archive source before promising the backfill.**

And the Aquarius half of that history is **regime-suspect**: emission movements recorded under
the outgoing voting system describe a mechanism being replaced. A perfect-foresight rotator
scored against them returns a large number measuring a game no longer on offer — the same class
of fiction as a gross-APY backtest, reached by a different route.

Which means the whole viability question is answerable **offline, with no capital, no
domain module and no population**:

1. **How often does the ranking flip, and by how much?** If one venue sits persistently
   above the others, there is no rotation alpha — allocate once and the null wins by
   construction. Rotation pays only if the ordering changes sign often enough, and by a
   margin wide enough to cover the cost of moving.
2. **What does a perfect-foresight rotator earn against the best static allocation?** This
   is the ceiling on the entire domain. An oracle that knows every future rate is an upper
   bound no achievable strategy approaches; if it beats sitting still by one percent of
   APY, the domain is dead before it starts.

**Measurement 2 is the kill criterion**, in the same spirit `MAKER.md` set one for its fill
model. Get the number before writing a line of `domain_yield.py`. Both measurements must be
computed *net* of the frictions in the next section — a gross-APY version of either will
produce a large and entirely fictional result.

---

## Three ways this fails quietly

### 1. Emission-token exit friction — this codebase has been burned by exactly this

A large share of the yield arrives denominated in AQUA and BLND, which must be sold into a
book to become returns. `friction.py:16` already records that the extra assets the
population keeps trying to admit have books far worse than XLM's; FUTURE.md's opening
section puts those legs at **151–186bp against XLM's 12**.

So the advertised APY and the realized APY differ by the cost of exiting the emission
token, and on those spreads that gap can be most of the edge. This is criterion 7, and it
is precisely the failure at the top of FUTURE.md: `execute_trade` and `backtest.py` both
bought and sold at one price, the fitness landscape was frictionless, and the loop selected
for churn — a leader turning over $5,757 for $23.33. A rotation backtest that scores gross
APY reproduces that mistake in a new venue.

A related trap: **the emission token's book depth caps the strategy's size**, independently
of any cap in `stellar_trader.py`. Earning 500 AQUA a week is only worth 500 AQUA if 500
AQUA can be sold without moving the book.

**Amendment: the friction is asymmetric, and that matters more than its average size.**
Aquarius yield is mostly AQUA, plus some in-kind fee income on the concentrated pools. Blend
pays interest in the supplied asset, and adds BLND emissions only on some pools, by a
configuration that rarely changes. So nearly all of Aquarius's advertised APY has to survive
the AQUA book to become a return, while most of Blend's arrives already denominated in what was
supplied. Net of friction the two venues' headline numbers are not comparable, and a ranking
computed on advertised APY has the sign of its own error built in. The BLND component, being
config-driven and stable, is a near-constant rather than a state variable worth predicting.

### 2. Withdrawal is not guaranteed, and that breaks an existing invariant

Every position this system holds today can be market-sold to flat. `wind_down` assumes it,
`MAX_STUCK_USD = 2.0` bounds the exception, and exceeding it prints `LIVE TRADING HALTED`.

A supplied position can be **temporarily un-withdrawable**: a lending pool at high
utilization has no free liquidity to pay a withdrawal with, and an AMM position exits at a
price with impermanent loss rather than at par. The existing machinery would read either as
unsellable notional and halt live trading.

So `domain_yield.py` needs stuck-semantics that distinguish *illiquid by design, will free
up as utilization falls* from *trapped*. That is a money-boundary design question and
belongs in `domain.py` group 4, not in a strategy's `main.py`. It is the single largest
piece of new safety work this domain requires, and it should be designed before the first
live allocation, not after the first halt.

**Amendment: this shrinks, and what remains splits in two.** Blend pools carry no lockup. The
backstop does, and backstop contributions are excluded from this model — which also keeps the
borrow facility `SHORTING_PLAN.md:7` rejected out of the design entirely. The Blend side of the
concern therefore reduces to utilization-driven illiquidity: transient, readable before
allocating, and boundable by sizing against free liquidity rather than by new stuck-semantics.
The Aquarius side does not reduce, because an AMM exit is a price and not a par redemption —
and that is a large enough problem to stand on its own below.

### 3. An Aquarius position is not a yield position

Aqua AMM pools cannot be entered single-sided. A position there is a yield position **plus a
short-volatility LP payoff**, and three consequences follow that the framing above hides.

It carries price exposure, so it is a non-base leg by any honest accounting — which puts it
under the cap discussed in "Capacity, honestly" below rather than outside it. Entering requires
swapping into the pair ratio and exiting requires swapping back, so every Aquarius rotation
crosses books twice, at XLM's 12bp at best and `friction.py:16`'s 151–186bp on the assets the
population keeps trying to admit. And impermanent loss must appear in measurement 2 as a term
rather than a footnote: it is the one cost that grows with exactly the volatility that makes
rotation look attractive in the first place.

The practical consequence is a narrowing. Only the low-IL subset of Aquarius is tractable —
stable pools and tightly correlated pairs — so step 1 has to record pool type and pair
composition, not address and rate alone. A 40-pool sample of the 337 the AMM API lists split
`constant_product` 30 / `stable` 6 / `concentrated` 4, so that subset is real but small.

---

## The horizon question

Rotation decisions arrive on a scale of days, not seconds, so the cull cadence has to
lengthen. Three things follow.

**Mechanically it is already free.** `RANK_GRACE_S` became a per-domain member in the
domain refactor (`domain.py:44`), and `domain_kalshi.py:106` already runs 24h against every
other domain's 3h. A several-day value needs no `monitor.py` change and no new contract
member.

**It makes criterion 5 worse, not better.** A longer horizon means fewer decisions per unit
time and therefore fewer independent bets. A low event rate cannot be fixed by waiting
longer — waiting longer is what a low event rate *forces*.

**But the noise asymmetry partly rescues it, and this is the interesting part.** Criterion 5
is a proxy for "is the ranking sampling noise?" In a price domain an hour of P&L is
dominated by price movement, which is why hourly rank over one asset's path is mostly
coin-flipping. **Interest accrual is nearly deterministic**: a strategy earned exactly what
the rate it was allocated to said it would. Signal-to-noise per observation is far higher,
so reliable ranking needs fewer events here than criterion 5's framing assumes.

The residual risk is different in kind, and worth naming separately: not that the ranking is
noisy, but that the scores are nearly **tied**, because every strategy collects roughly the
base yield and the differences between them are small relative to it. That is a *separation*
problem rather than a noise problem, and measurement 2 above is exactly the test for whether
it bites.

**The real cost is generation count.** Culling every three days yields ~10 generations a
month instead of ~700. And the loop still cycles hourly — cloning, revising and spawning —
so roughly 72 revisions accumulate per scored generation, flooding the population with
strategies sitting at `STARTING_SCORE`, where ranking falls through to the action-count
tiebreak at `monitor.py:1540`. **Decoupling revision cadence from cull cadence is a
`monitor.py` change, not a domain constant**, and it is the one piece of real loop
machinery this domain needs that no existing domain has required.

Note what that turns the system into: LLM-directed search with occasional selection, rather
than evolution. Criterion 9 arguably wants that anyway — it prefers domains where
`_run_revision` rather than `tweak_config` is the operator that matters — but it should be a
decision rather than a side effect.

One smaller trap in the same area: `is_idle` (`monitor.py:430`) demotes any strategy that has
never logged an action, and the rank sort prefers higher action counts among equals. In a
yield domain the correct behaviour is frequently *to hold*, so `activity()` must count the
initial allocation as an action, and the tiebreak's mild reward for churn is a cost here with
no offsetting benefit.

---

## Capacity, honestly

At roughly $60 of capital, a perfectly captured 5% differential is a few dollars a year.
Criterion 8 applies with full force and the answer is the one it prescribes: the reason to
run this is **what it teaches**, not what it earns.

Two things soften it slightly relative to the rest of FUTURE.md's list — and **both are
Blend-only**, where the original text claimed them for the domain as a whole.

Supply-side lending can deploy the *whole* balance rather than the $8 that
`MAX_TOTAL_NONBASE_EXPOSURE_USD` (`tools/stellar_trader.py:75`) allows a trading strategy, so
capacity binds less tightly there than anywhere else on the list. An Aquarius LP position is
two-sided and price-exposed, so it is precisely the kind of leg that cap exists to bound.

And rotation *between Blend pools* costs Stellar transaction fees — fractions of a cent —
rather than the 10–20bp round trip that makes FUTURE.md item 10's perp version structurally
negative at this size. Rotating into or out of Aquarius costs two book crossings, which is
squarely the regime item 10 was rejected for.

---

## What to do, in order

1. **Confirm the venues.** Enumerate the Blend pools (YieldBlox, Fixed) and the Aquarius
   reward pools, their contract addresses and their current rates, by contract simulation.
   `tools/reflector_oracle.py` is the precedent for confirming constants live rather than
   scraping docs — read-only, costs nothing, signs nothing. The `stellar` CLI is no longer the
   only route: `stellar-sdk` and `aquarius-sdk` are now installed, so simulation can run
   in-process. Record pool type and pair composition on the Aquarius side, per §3. Then add
   one reading that needs no archive at all: **how far Blend supply APY has actually moved**
   across the two pools over the ~7-day RPC window. If Blend utilization is itself sticky, the
   domain is dead without the backfill ever being built.
2. **Backfill history.** Reconstruct APY, emission rate and utilization per venue over as long
   a window as the chain allows — which starts with choosing an archive source, since RPC holds
   a week. Treat pre-change Aquarius emissions as a description of a retired mechanism rather
   than as evidence about the next one.
3. **Run the two measurements**, net of emission-exit friction sized from the real AQUA and
   BLND books rather than assumed, and net of IL and two-sided entry cost on any Aquarius leg.
   **Stop here if measurement 2 is small.**
4. Only then: the withdrawal/stuck boundary design, `domain_yield.py`,
   `template_repo_yield/`, and the revision-vs-cull decoupling in `monitor.py`.

Steps 1–3 need no capital, touch no money boundary, and cannot dirty a watched repo. They
are worth doing on those grounds alone — the backfilled rate history is a reusable artifact
even if the domain never ships.

---

## Open questions

Recorded because they are assumptions rather than measurements, and each could change the
plan:

- Aquarius emission mechanics: the voting period length, when a vote snapshot binds, and how
  far ahead the next period's emissions are actually knowable. The entire criterion 6 argument
  rests on this lag being real and non-trivial.
  Answer: aquarius's voting system is changing.  The reward mechanics will
  be much less dynamic in the future.
- Whether Blend pool participation carries any lockup, cooldown or withdrawal delay beyond
  utilization-driven illiquidity.
  Answer: blend pool has no lock-up.  The backstop _does_, but we're not
  including backstop contributions in this model.
- Whether AMM participation on the Aquarius side means impermanent loss must be modeled, or
  whether reward pools can be entered on a single-sided basis. IL materially changes
  measurement 2 and is easy to omit by accident.
  Answer: Aqua AMM pools cannot be entered one-sided.  IL must be considered.
- How much of each venue's yield is emission-denominated versus paid in the supplied asset.
  This ratio determines how much of the friction in §1 above actually bites.
  Answer: For aqua, it's mostly AQUA and -- in the case of concentrated AMMs --
  partially in the assets themselves from fees.  For Blend, you earn interest
  on deposits, and sometimes earn BLND emissions (this changes based on pool
  configuration, but does not change often).

Those answers raised one more, and it is now the load-bearing one:

- **How far along is the Aquarius voting change** — announced, in progress, or already live?
  It decides whether historical Aquarius emission data can be used for measurement 2 at all,
  or whether that half of the backfill should be skipped as a measurement of a regime that no
  longer exists. Everything else in step 1 can proceed without the answer.

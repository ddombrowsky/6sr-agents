# YIELD.md — yield rotation across Stellar's lending and reward venues

**Written 2026-08-21.** A candidate domain: allocate capital across Aquarius reward pools
and Blend's two main lending pools (YieldBlox and Fixed), rotating as their rates move.
This document is the case for investigating it, the two measurements that decide it, and
the two ways it could quietly fail. It is *not* an implementation plan — `MAKER.md`'s
phase structure would be premature here, because the cheap test below can end the item.

FUTURE.md's ranked item 9 is the one-line version. This is the argument.

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

---

## The two measurements that decide it

The reason to take this seriously ahead of the other candidates: **pool state is on chain
and historical, so it is backfillable.** Unlike the maker, which needed weeks of
`market_recorder.py` before any backtest could exist, months of APY, emission and
utilization history should be reconstructible immediately. Criterion 2 comes nearly free.

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

## Two ways this fails quietly

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

Two things soften it slightly relative to the rest of FUTURE.md's list. Supply-side yield can
deploy the *whole* balance rather than the $8 that `MAX_TOTAL_NONBASE_EXPOSURE_USD` allows a
trading strategy, so capacity binds less tightly here than anywhere else on the list. And
rotation costs are Stellar transaction fees — fractions of a cent — rather than the 10–20bp
round trip that makes FUTURE.md item 10's perp version structurally negative at this size.

---

## What to do, in order

1. **Confirm the venues.** Enumerate the Blend pools (YieldBlox, Fixed) and the Aquarius
   reward pools, their contract addresses and their current rates, by contract simulation.
   `tools/reflector_oracle.py` is the precedent: the `stellar` CLI is installed, simulation
   is read-only, costs nothing and signs nothing, and that file's docstring documents the
   convention of confirming constants live rather than scraping docs.
2. **Backfill history.** Reconstruct APY, emission rate and utilization per venue over as
   long a window as the chain allows. This is the artifact everything else depends on.
3. **Run the two measurements**, net of emission-exit friction sized from the real AQUA and
   BLND books rather than assumed. **Stop here if measurement 2 is small.**
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
- Whether Blend pool participation carries any lockup, cooldown or withdrawal delay beyond
  utilization-driven illiquidity.
- Whether AMM participation on the Aquarius side means impermanent loss must be modeled, or
  whether reward pools can be entered on a single-sided basis. IL materially changes
  measurement 2 and is easy to omit by accident.
- How much of each venue's yield is emission-denominated versus paid in the supplied asset.
  This ratio determines how much of the friction in §1 above actually bites.

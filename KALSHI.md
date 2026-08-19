# KALSHI.md — implementing prediction markets as a second domain

**Written 2026-08-10.** Implementation plan for FUTURE.md's recommended-order item 4:
"Prediction markets as a new domain. Use the kalshi public API to get real prediction
markets." This doc is the engineering breakdown of that one line, grounded in the actual
`master_agent/domain.py` contract and the `domain_forecast.py` implementation that
preceded it — not a redesign of either.

## OUTCOME — closed 2026-08-18. Not a viable domain for profit.

**Everything below this section is the plan as written on 2026-08-10, kept as history.**
Phases 0 and 1 shipped essentially as specified; Phase 2 was never started and **should
not be**. The live run on `ssr_agent01` ran ~7 days (2026-08-11 → 2026-08-18) and was
stopped deliberately. Do not spend further time refining `domain_kalshi.py`, its scoring,
or the population in the expectation that money comes out of it. It does not, and the
reason is structural rather than a tuning problem.

**The machinery is not what failed.** The domain contract carried a third domain with none
of sdex's furniture, `monitor.py` ran it for a week with no special-casing, and
`selftest_domain.py` guarded the swap. This is a negative result about the *market*, not
about the loop. That distinction is the reusable part.

### 1. No persistent edge ever existed — the leaderboard was measuring noise

The six highest-scoring strategies of the whole run peaked at scores of 1314–1650, which
implied Brier edges of +0.006 to +0.013 over the frozen market price. Now that each has
180–224 resolved `(ticker, hour)` buckets, every one of them sits between **−0.0003 and
−0.0007** — i.e. slightly *worse* than simply parroting the market. The peaks were
small-sample flukes that regressed past zero as evidence accumulated. The strategy leading
at shutdown (`clone_baa3c857e802`, score 1034.24) had an edge of +0.0010 over 48 buckets,
which is the same story caught earlier in its life.

This is the same noise domination `domain_forecast.py`'s run showed. Here it is worse,
because Kalshi markets resolve on a scale of hours-to-days, so the evidence needed to tell
skill from luck accumulates far more slowly than the loop's cull cadence.

### 2. Even a perfect forecast would not have paid — friction exceeds the mispricing

Calibration of `last_price` against realized outcomes, snapshotted ≥6h before close over
234 resolved markets: every band is calibrated to within its own noise. The only band with
a real sample (px ≤ 0.05, n=139) is mispriced by **1.4¢** — the classic favourite-longshot
bias, in the expected direction.

Against that 1.4¢ of available gross edge:

- Kalshi taker fee `ceil(0.07 × C × P × (1−P))` is **≥1¢ per contract**.
- Median bid/ask spread at forecast time is **1.0¢** (mean 3.2¢).

So the bias is real, decades-documented, publicly known — and still unharvested, because
harvesting it costs more than it pays. Backtested net of both frictions: "sell longshots"
−0.5%, "buy NO on everything" −4.3% to −7.1%, "buy YES on everything" −12% to −13%. ("Buy
favourites" showed +3–6% on n=2–11, which is noise, not a finding.)

Reconstructing the leader as if it had traded for real (1 contract per market, hold to
settlement): filling at last price with no fees, −$0.12 on $3.12 staked; crossing the
spread and paying fees, **−$1.61 on $5.61 staked, −28.7%**. All eight live strategies came
out between −5% and −39% on realistic fills. A positive Brier edge and a negative P&L at
the same time is not a contradiction — it is the whole lesson.

### 3. The genome had no information the price did not already contain

`decide(market, history, state, config)` receives one market row and the strategy's own
past `implied_prob` readings for that ticker. That is it. It is technical analysis on a
price series, and the inputs are a strict subset of what the price already reflects. No
rule over those inputs can have a positive expected edge against the market that produced
them. Evolution can select noise from such a population, but it cannot manufacture
information — which is exactly what §1 shows it did.

This is the root cause. §1 and §2 are consequences.

### 4. The scoring objective was never profit

`score_path()` ranks on shrunk Brier edge versus the frozen market price. That is a proper
scoring rule and it is correctly implemented — but it is not P&L, and the two came apart
in practice (§2). Optimising it harder selects for parroting the market with small
deviations, which is precisely what the population converged on: the leader reproduced
`p_market` exactly on 76.6% of its rows, and deviated by a median of 1.2¢ when it did
deviate — against a 1.0¢ spread and a ≥1¢ fee.

If this domain is ever revived, score net-of-friction P&L instead. That is a contained
change (the forecast row would need `yes_bid`/`yes_ask` frozen alongside `p_market`, which
`.kalshi_market_history.jsonl` already records per poll), and it makes "do not trade" a
representable action. **It does not create an edge** — it only stops the loop rewarding a
metric uncorrelated with the goal.

### 5. Capacity ceiling — the prize was never large

Across the recorded Climate-and-Weather dailies: median market volume **647 contracts**,
p90 6,648, median open interest 529. Even a fat 5% net edge on a p90-sized market is tens
of dollars. These markets cannot support a meaningful return regardless of skill, which is
worth weighing before any future effort is spent here.

### What would have to be true to revisit this — and what is explicitly deferred

The 1.0¢ median spread on daily weather markets is itself the tell: something is standing
there quoting both sides tightly, and it is almost certainly a professional operation
already pricing the same free NWS data any strategy here would scrape. Out-forecasting it
on public information is not a realistic plan. Three things could change the picture, in
descending order of realism:

1. **Market making — the one worth coming back to, but NOT now.** Structurally different
   from everything attempted here: the spread we measured as a *cost* is somebody's
   *revenue*, and the edge requires no forecast at all. Kalshi maker fees are zero or
   near-zero on most series (verify before relying on it), which inverts the arithmetic
   that killed §2. It needs Phase 2 signed execution, resting-order management, and an
   adverse-selection model — none of which can be backtested from the data this run
   produced, since we only ever recorded top-of-book snapshots at a 300s cadence.
   **Deliberately deferred.** Revisit as its own design exercise, not as a revision of
   this plan; nothing in Phase 2 below is the right starting point for it.
2. **Exogenous information** the market has not yet priced — for weather dailies, NWS/NDFD
   gridded forecasts or METAR observations. This is the only route that creates a
   *forecasting* edge, and it is also the one most likely already occupied (see above).
3. **Under-covered markets.** The useful question was never "what do we know that the
   market maker doesn't," but "which markets is nobody bothering to quote." That is a
   different search, and the capacity ceiling in §5 applies to it doubly.

### What the run left behind, and what was preserved

Stopped cleanly on 2026-08-18: `emperor` stopped via supervisor, 22 strategy processes
wound down through `strat_manager.py stopall`, both Kalshi daemons killed, watched repos
verified clean. Nothing was deleted. Still on disk under `v/`: 125 strategy directories,
`trades/*.log` (307 resolved tickers), `.kalshi_market_history.jsonl` (39,251 poll rows,
238 resolved markets with full bid/ask history) and `emperor_logs/`. That dataset is the
one durable asset this run produced — any future analysis of Kalshi weather pricing should
start from it rather than re-recording.

---

## Status this plan assumes

*(Historical — this section describes what was true on 2026-08-10, before the run. See
OUTCOME above for how it turned out.)*

- The domain-plugin architecture (`domain.py`, `domain_sdex.py`, `domain_null.py`) is
  built and self-tested (`selftest_domain.py`, 414 checks).
- `domain_forecast.py` + `tools/forecast_engine.py` proved the generic loop can improve a
  population on a domain that isn't SDEX: the run on this container (ssr_agent01) spans
  2026-08-07 23:21 through 2026-08-10 11:13 and every tracked strategy beats the
  base-rate null (brier ≈ 0.21–0.23 vs. base 0.2500). That was the empirical prerequisite
  FUTURE.md's own `[DONE]` marker on item 3 didn't actually carry evidence for until now
  — worth remembering next time a recommended-order item is trusted at face value. Read
  this result as weaker than it first sounds, though — see "Lessons from
  `domain_forecast.py`'s actual run" below: its revision prompts went near-content-free for
  roughly two-thirds of that window, so this is stronger evidence for the shrinkage math
  and template defaults than for sustained LLM-driven revision quality.
- No Kalshi code exists anywhere in this repo yet (`grep -ri kalshi` on `v/` is empty
  outside this file). Clean slate.

## API access — confirmed, not assumed

Checked `docs.kalshi.com` directly on 2026-08-10:

- Kalshi exposes **public REST endpoints that need no API key and no account**: events,
  markets, order books, candlesticks, trades, all free reads at roughly 20 req/s. Base
  `https://external-api.kalshi.com/trade-api/v2`. This covers everything Phase 1 below
  needs — live market data and (per the docs' listed endpoint set) candlesticks/trades
  history for resolved markets.
- **Placing real orders needs an account and an API key**: RSA 2048-bit PKCS#8 key pair
  generated in the Kalshi dashboard, requests signed with `KALSHI-ACCESS-KEY` /
  `-TIMESTAMP` / `-SIGNATURE` headers. That's a KYC'd trading account, not a dev signup —
  a real onboarding step, gated to Phase 2 below and not needed to start.
- A demo/sandbox environment exists (`external-api-ws.demo.kalshi.co`) with simulated
  liquidity, functionally identical API shape. Useful for Phase 2 order-flow testing
  before pubnet-equivalent real money, the same role `PAPER_ONLY=1` plays for
  `stellar_trader.py`.

## The core design problem this domain has that `domain_forecast` didn't

`forecast_engine.py`'s questions are synthetic and resolve **instantly, every tick** —
`question_at()` is a pure function of a seed, so `submit_forecast()` can score and log a
Brier value the moment a strategy answers. That's what let `score_path()` be simple: read
an append-only log of already-resolved outcomes.

Real Kalshi markets don't do that. Most resolve over days to weeks. `CYCLE_SLEEP` is
3600s and the cull is hourly rank-based — a strategy scored only on markets it has
personally seen resolve would go through many culls with near-zero resolved evidence,
which is precisely the criterion-1 failure FUTURE.md warns about ("a domain whose
feedback takes a week cannot be scored by this loop at all"). FUTURE.md's own caveat on
this item says as much: *"score on mark-to-market plus running calibration, not
resolution alone."*

So the scoring design is the one piece with no existing template to copy. The approach:

- **Primary signal: mark-to-market log-loss/Brier against the market's own implied
  probability**, recorded at forecast time, not at resolution time. A strategy states
  `p_hat` for a market currently trading at implied probability `p_market` (last trade
  price or best bid/ask midpoint, whichever the API cleanly exposes); score that instant
  against a proper scoring rule versus `p_market` as the comparison point — this is
  literally FUTURE.md's stated null for this domain ("the market's implied probability").
  Beating it means the strategy's read is better than the crowd's, which is real skill,
  not luck, on a market with genuine trading volume.
- **Secondary signal, once available: true Brier at resolution.** A background
  reconciliation job (see Phase 1) periodically checks markets a strategy has forecasted
  on and, once Kalshi reports a settlement, retroactively records the resolved outcome.
  This is not optional cosmetic detail — it's the check that the mark-to-market proxy
  isn't systematically gameable (e.g., a strategy that just parrots the market price would
  score ~0 edge on the mark-to-market metric by construction, which is correct, but a
  strategy that's cheaply beating the mark-to-market number needs its calibration checked
  against ground truth eventually or the loop is optimizing a proxy that never gets
  audited).
- This makes `score()` vs `score_path()` diverge here even more than they do in
  `domain_forecast.py`: `score_path()` should be authoritative and read a log that mixes
  mark-to-market entries (available immediately) and resolved entries (available late),
  weighted so a strategy isn't punished for not yet having resolutions — mirror
  `_shrunk_edge()`'s prior-weighting pattern from `domain_forecast.py`, blending toward
  the mark-to-market proxy when resolved-sample count is low and toward true Brier as it
  accumulates.

## Lessons from `domain_forecast.py`'s actual run, not just its code

The Status section above cites the 2026-08-07 23:21–2026-08-10 11:13 run as the empirical
proof this loop works on a non-sdex domain. Auditing `master_agent`'s git history for that
window (not just reading the current file) surfaces three things worth knowing before
`domain_kalshi.py` repeats the pattern — none of these are visible from `domain_forecast.py`
as it reads today, because in each case the file that changed is not the one you'd think to
check.

- **The self-revision pass edited `domain_forecast.py` itself, not just strategy code —
  and what it added works against a real-money promotion gate.** `emperor.sh`'s docstring
  names four files the revision pass is supposed to touch (`master-agent.py`, `monitor.py`,
  `sr_agent_tools.py`, `strat_manager.py`); nothing in code enforces that boundary, and
  commits `20df197`/`e8b932d` ("added score time degredation" / "added note about confidence
  cap", 2026-08-07 23:17–23:38) show it reaching into `domain_forecast.py` and adding
  `score_timeout_factor()` — a multiplier that decays `score_path()`'s output to zero
  between 24h and 72h of a strategy's forecast count, justified in its own docstring as
  "this is a test domain, after all... leave room for new clones." That justification does
  not carry over: Phase 2's `qualifies_for_live()` gate explicitly wants "minimum
  resolved-forecast count and age" as a *positive* signal, so a decay mechanism shaped like
  this one would actively fight the promotion gate rather than serve it. Two things to
  decide before `domain_kalshi.py` exists, not after: whether the revision pass gets a real
  boundary around domain modules (today it doesn't, for either domain), and — regardless of
  that answer — do not port `score_timeout_factor`'s shape when mirroring `_shrunk_edge()`.
- **The forecast-domain revision prompts were gutted mid-run and never restored — the
  validation run's back half was not testing what the Status section implies.** Commit
  `6c8d948` ("reset forecast prompts, they aren't working", 2026-08-08 09:53, roughly 10.5
  hours into the cited run) collapsed `_build_forecast_revision_system_prompt()`,
  `_refine_prompt_forecast()`, and `_explore_prompt_forecast()` in `master-agent.py` — around
  170 lines covering the `decide_source` gotchas, config-validation rules, and the
  "A SUMMARY IS NOT A CHANGE" anti-pattern — down to a single line each:
  `'adjust config.json to increase your performance'`. That is still the state of
  `master-agent.py` at HEAD today. So roughly two-thirds of the cited run had the revision
  LLM working from a near-content-free prompt, not the engineered one — the "every tracked
  strategy beats the base-rate null" result is real, but is better read as evidence the
  shrinkage math and template defaults are sound under light mutation, not as sustained
  proof of LLM-driven revision quality. For `template_repo_kalshi`/`domain_kalshi.py`'s
  `prompt_facts()`-driven prompt: expect the same failure mode to recur (nothing currently
  catches "the revision prompt got replaced with something useless" — the smoke test only
  checks that a revised `main.py` runs), and don't treat a long unattended run as validation
  without first diffing `master-agent.py`'s prompt-building functions across the run window.
- **The mark-to-market/reconciliation log needs forecast_engine.py's TRUST MODEL disclosure
  made explicit for Kalshi, at real-money stakes instead of hypothetical ones.**
  `tools/forecast_engine.py`'s docstring states plainly that the revision LLM "runs with
  full read/exec access as root" and could in principle read `LIVE_SEED` and the true
  probability straight out of the module instead of forecasting honestly — undefended on
  purpose, because nothing of value is at stake, closing with: "If this benchmark is ever
  used for anything higher-stakes than measuring whether the evolutionary loop improves a
  paper score, that is the seam to hang a real secret on." Kalshi Phase 2 is exactly that
  higher-stakes use, and the equivalent seam is `kalshi_recorder.py`'s and
  `kalshi_reconcile.py`'s log files: a revision pass with root access could edit a resolved-
  outcome entry directly to fabricate the track record `qualifies_for_live()` checks, and
  the first bullet above is direct proof this system's revision pass does reach into files
  its own docs don't list as fair game when it decides to. `check_boundary_integrity()`
  (sdex's real defense, watching `/opt/tools`'s git history) needs to explicitly cover these
  two logs before Phase 2, not just the recorder/reconciler processes' liveness — process
  supervision (`ensure_background_jobs()`) answers "is the daemon running," not "did anything
  edit its output by hand."

## Phased plan

### Phase 0 — spike, no code committed to the loop

**Answered 2026-08-10**, against the live public API (`api.elections.kalshi.com` /
`external-api.kalshi.com`, both reachable from the host and from inside `ssr_agent01`).
Full findings below; each answer changes `domain_kalshi.py`'s shape as anticipated.

1. **Which endpoint gives implied probability cleanly?** — **`last_price_dollars`,
   filtered by volume/open interest.** Every market object (list and single-market
   endpoints) carries `last_price_dollars`, `yes_bid_dollars`, `yes_ask_dollars`. But the
   order book is often one-sided: `GET /markets/{ticker}/orderbook` on
   `KXHIGHCHI-26AUG10-B86.5` returned an empty `yes_dollars` side with only `no_dollars`
   populated, so a bid/ask midpoint isn't always computable — a real instance of the
   thin-book gameability `dex_price.py`'s docstring warns about, not a hypothetical.
   `last_price_dollars` is always populated, including `0.0000` for a market with no
   trades yet, which is itself a useful signal ("nothing has priced this"). Use
   `last_price` as the primary read, gated by a minimum `volume_fp`/`open_interest_fp`
   floor to exclude untraded markets — this is the concrete mechanism for the
   "Thin-market gaming risk" item under Open Risks below, not a separate design.
2. **Which categories/markets give enough independent bets per hour?** — **Crypto,
   Financials, and Climate/Weather have real short-cadence series; weather is the safer
   Phase 1 starting filter.** Pulled all 12,635 series from `GET /series` and
   cross-tabbed category × frequency. Short-cadence counts: `Crypto` (fifteen_min: 14,
   hourly: 25, daily: 22 series), `Financials` (hourly: 18, daily: 38), `Climate and
   Weather` (daily: 67). `Sports` has the largest raw count (2,360 series) but almost
   entirely at `custom` cadence, i.e. unreliable timing — not useful for volume in
   `domain.py`'s criterion-5 sense. Frequency-of-resolution doesn't guarantee liquidity
   at every instant, though: freshly-opened `KXHYPE` (crypto, hourly) markets showed
   `last_price=0.0000, volume=0.00, open_interest=0.00` right at the top of the hour —
   empty book, no signal yet. `KXHIGHCHI` (Chicago daily high temp) by contrast carried
   real volume (1,000s) and open interest hours before close. Recommend `Climate and
   Weather` dailies as `category_filter`'s Phase 1 default; crypto hourlies need an
   "open for at least N minutes" gate before `main.py` trusts their `last_price` at all.
3. **Is there a resolved-markets history endpoint usable for a fixed offline backtest
   set?** — **Yes, unconditionally, no forward-recording needed.** Confirmed on
   `KXHIGHCHI-26AUG09-T90` (settled the prior day): the single-market endpoint returns
   `status: "finalized"`, `result: "no"` indefinitely by ticker; `GET
   /markets/trades?ticker=...` returns the full trade tape; `GET
   /series/{ticker}/markets/{ticker}/candlesticks` (requires `start_ts`/`end_ts` as unix
   timestamps — 400s without them) returns full OHLC + bid/ask history. `kalshi_backtest.py`
   can build its fixed replay set directly from markets that are already closed today; the
   Phase 1 timeline risk this question was gating does not apply.
4. **Rate limits at population scale.** — **No enforced ceiling observed at ~12 req/s;
   the single-writer recommendation stands regardless of the exact number.** No
   `X-RateLimit-*` response headers on any call. A 25-request sequential burst
   (~11.6 req/s, network-latency-bound, no client-side throttling) returned 200 on every
   request — consistent with docs.kalshi.com's ~20 req/s figure. Deliberately didn't push
   further to find the actual ceiling; load-testing a production exchange to locate its
   break point isn't worth doing for a number that doesn't change the design decision.
   `kalshi_recorder.py`'s single-writer pattern (mirroring `market_recorder.py`) stays the
   default regardless of exactly where the ceiling sits, same reasoning as originally
   stated.

Concrete field names confirmed live, for `tools/kalshi_api.py` to use directly:
`last_price_dollars`, `yes_bid_dollars`/`yes_ask_dollars`, `volume_fp`,
`open_interest_fp`, `status` (`"active"` / `"finalized"`), `result` (`"yes"` / `"no"` /
`""`), and candlesticks' required `start_ts`/`end_ts` query params.

### Phase 1 — money-free `domain_kalshi.py`, mirroring `domain_forecast.py`'s shape

No real orders, no KYC account needed yet. Goal: repeat the forecast domain's validation
— run 24h+, confirm the population's mark-to-market score beats the market-implied-
probability null — before any money-boundary code is written at all, same reasoning as
not building item 4 until item 3 had a real result.

New files, following existing naming/placement conventions:

- **`tools/kalshi_api.py`** — thin, dependency-free (mirrors `dex_price.py`/
  `price_feed.py`'s style: keyless GETs, returns `None` on any failure rather than
  raising). `list_open_markets(category=None)`, `get_market(ticker)` →
  `{implied_prob, volume, close_time, ...}`, `get_resolved(ticker)` → outcome once
  settled.
- **`tools/kalshi_recorder.py`** — single-writer daemon, direct structural copy of
  `market_recorder.py`'s pattern: one process polls the open markets a strategy
  population is actually forecasting on, writes a durable log, everyone else reads it.
  Supervised by `domain_kalshi.ensure_background_jobs()` the way
  `monitor._ensure_recorder` supervises `market_recorder.daemon`. This is the piece
  `domain_forecast.py` didn't need (`ensure_background_jobs` is a no-op there because the
  question generator has nothing to supervise) — Kalshi's questions are real external
  state, not a pure function of a seed, so `background_jobs_alive()` must return `False`
  when this daemon is down rather than `True` unconditionally.
- **`tools/kalshi_reconcile.py`** — the resolution-tracking job from the design-problem
  section above. Walks each strategy's forecast log, finds entries whose market has since
  settled, appends the true-outcome Brier. Also a background job, also supervised.
- **`tools/kalshi_backtest.py`** — `forecast_backtest.py`'s counterpart: replay against a
  cached, fixed set of already-resolved markets (built once per Phase 0 answer #3),
  `importability_report()` gating `decide()` the same way.
- **`template_repo_kalshi/main.py`** — direct structural port of
  `template_repo_forecast/main.py`'s tick loop: `decide(market, history, state, config)`
  returns `p_hat` for one open market at a time, `config.json` carries
  `confidence_gain`-equivalent calibration plus `markets_per_tick` (volume knob) and
  `category_filter` (the "genuinely new direction" mutation knob — `domain_forecast`'s
  `inject_experiments` had only one lever; here category selection is the natural second
  one, since specializing in one category vs. spreading across several is a real
  strategic choice a revision LLM can reason about, not just a magnitude tweak).
- **`master_agent/domain_kalshi.py`** — the contract wrapper. Every member below has a
  direct `domain_forecast.py` analog to start from (function name → what changes):
  - `observe()`/`observe_population()` → read `kalshi_recorder`'s log instead of calling
    `forecast_engine.current_tick()`; `None` if the recorder is stale/down (criterion 1's
    "one cheap reading of the world", same fail-open-on-tooling policy).
  - `score()`/`score_path()` → the mark-to-market/resolved blend described above, in place
    of `_shrunk_edge()` over pure Brier. Keep the same shrinkage-toward-prior shape
    (`CONFIDENCE_PRIOR_N`/`CONFIDENCE_CAP` equivalents) — `domain_forecast.py`'s own
    docstring has a dated, measured example of what happens without it (a 3210-forecast
    strategy permanently outranking a genuinely-better 1854-forecast one).
  - `replay()`/`importability()` → `kalshi_backtest.py`, same shape as
    `forecast_backtest.py`'s.
  - `caps()` → `None` (Phase 1 has no money, same as `domain_forecast.caps()`).
  - `can_execute_live()` → `False` unconditionally, same honest-always-false pattern,
    until Phase 2 exists.
  - `SMOKE_ENV` → `{}` in Phase 1 (nothing to suppress), becomes real in Phase 2.
  - `normalize_config`/`sanitize_config`/`repair_config`/`config_is_sane`/
    `seed_config`/`tweak_config` → direct ports of `domain_forecast.py`'s, swapping
    `confidence_gain`/`questions_per_tick` for this domain's knobs plus validating
    `category_filter` against the known category list.
  - `prompt_facts()` → market-implied-probability null explanation, category list,
    volume/calibration knob bounds — same rule as `domain_forecast.py`: any number stated
    to the revision LLM is read live through here, never written as prose in
    `master-agent.py`'s prompt.

**Validation gate before Phase 2 starts:** run this domain the same way `DOMAIN=forecast`
was run — 24h+, multiple revision cycles, and confirm the population's mark-to-market
score beats the market-implied-probability null with resolved-market data backing at
least some of it up. Don't start Phase 2 on the strength of the mark-to-market proxy
alone; that's the whole point of the reconciliation job.

### Phase 2 — real execution (real money, later, separate decision)

> **NOT STARTED, AND NOT TO BE STARTED.** Superseded by OUTCOME above: Phase 1 produced
> no edge, so the validation this phase was explicitly gated on never landed. Kept for the
> record. If market making is ever revisited (OUTCOME item 1), design it fresh — a
> taker-shaped execution path is the wrong starting point for a maker strategy.

Not designed in detail here — sequencing note only, gated on Phase 1's validation
actually landing:

- `tools/kalshi_trader.py`, structural copy of `stellar_trader.py`'s shape: hard caps as
  module constants only, never caller-supplied, never config-overridable
  (`MAX_TRADE_USD`-equivalent, `MAX_DAILY_USD`-equivalent, a per-market position cap).
  `can_execute_live()` becomes a real check (does `main.py` call the execute path?),
  fails closed like `domain_sdex.can_execute_live()` does.
  `SMOKE_ENV = {'PAPER_ONLY': '1'}`-equivalent so a smoke-tested revision structurally
  cannot place a real order.
- Real order placement needs the KYC'd account + RSA key pair from the API-access
  section above — a manual onboarding step outside the code, do this once, well before
  it's needed, since it's not automatable the way the read-only endpoints are.
- Promotion gate shaped like `qualifies_for_live()`: minimum resolved-forecast count and
  age, score above baseline, structural check that `main.py` actually executes.
  `promotion_sizing()`/`prepare_live()`/`retire_live()` — Kalshi has no trustline-opening
  equivalent (no Stellar-style asset admission step), so these are simpler than sdex's,
  but `retire_live()` still needs a wind-down equivalent: close/exit any open positions
  before handing the live flag to a different strategy, the same reasoning
  `wind_down()`'s docstring gives for not stranding a real position.
- One domain per process today (`DOMAIN` env var, per `domain.py`'s "what is deliberately
  not here" section) — running SDEX-live and Kalshi-live simultaneously needs the
  cross-domain leaderboard/per-domain live slot work FUTURE.md defers, not something to
  build speculatively here.

## Open risks worth tracking, not solving up front

*(Historical. The thin-market risk below was handled by `kalshi_recorder.py`'s MIN_VOLUME
floor and did not bite. The risk that actually killed the domain — that a well-made market
leaves no edge for a price-only genome — is not on this list, which is the most useful
thing about it.)*

- **Thin-market gaming risk**, same shape as `dex_price.py`'s mid-price warning: a
  strategy could favor markets with wide spreads/low volume where the mark-to-market
  proxy is noisy, then look skilled by construction. Filter Phase 1's market selection by
  a minimum volume/liquidity floor the way sdex's marks are depth-capped, not left
  unbounded.
- **Category concentration vs. criterion 5.** If the loop discovers only one category has
  enough resolution-cycle frequency to score well, the population converges to it and the
  domain stops being "genuinely different from price trading" in practice even though it
  is in code — worth watching in the same spirit `TEMPLATE_SPAWNS_PER_CYCLE` exists to
  keep sdex's population from narrowing around current leaders.
- **The reconciliation job is new infrastructure with no existing precedent in this
  codebase** (nothing else here tracks "an external fact will become knowable at an
  unpredictable future time and a log needs updating in place"). Budget real design time
  for it; don't treat it as a footnote to `domain_kalshi.py` itself.

## Recommended order

*(Historical. Steps 1–6 were completed; step 7 was correctly never reached, because step 6's
bar was not cleared — see OUTCOME above.)*

1. Phase 0 spike (endpoint confirmation, category selection, rate-limit sanity check) —
   no committed code, answers change the plan above.
2. `tools/kalshi_api.py` + `tools/kalshi_recorder.py`, tested standalone against the live
   public API before anything touches the loop.
3. `template_repo_kalshi/` + `master_agent/domain_kalshi.py`, run through
   `selftest_domain.py`'s pattern (or a Kalshi-specific differential check) before it
   touches a real `monitor.py` cycle.
4. `tools/kalshi_backtest.py`, wired to `check_replayable`/`importability`, gating
   revisions before the first real cycle runs.
5. `tools/kalshi_reconcile.py`, running alongside the recorder from day one of Phase 1 —
   not bolted on after the mark-to-market-only version looks like it's working.
6. Run `DOMAIN=kalshi` for 24h+, multiple revision cycles, same bar
   `domain_forecast.py`'s prerequisite run cleared.
7. Only then: Phase 2 design doc, KYC account setup, `kalshi_trader.py`.

# Paper-funded XLM shorting — implementation plan

Written 2026-08-11. Supersedes the original note in `shorting.txt` and answers the
concern `FUTURE.md` item 4 raised against it (a paper mechanism that "breeds strategies
that cannot be executed live"). The difference: this design is XLM-only, capped at a
fixed 6000 XLM paper borrow rather than a flat -$200 across all assets, and the live side
is not Blend or any other lending protocol — it's real, pre-funded XLM inventory in
claudio's own wallet that a short sells into and a cover buy restores. Paper and live use
the literal same mechanism at two different scales, the same relationship every other
paper/live pair in this system already has.

## Design summary

- **Paper**: `borrowed_xlm` is a new top-level `state.json` ledger, kept separate from
  `positions['XLM']` — `portfolio.add_amount` hard-clamps at zero and that invariant
  can't be touched without breaking every existing long-only reader. A sell beyond what's
  held draws against a 6000 XLM ceiling instead of clamping to zero; a later buy repays
  the borrow before it ever adds to the real position.
- **No `decide()` contract change.** Shorting is a config flag (`allow_shorting`) plus a
  capability inside `execute_trade`. An existing strategy's `decide()`/`decide_asset()`
  keeps returning plain `('sell', 'sell', usd)` exactly as today — `execute_trade`
  decides whether that sell draws from real holdings or the borrow facility. Any of the
  ~90 existing strategies gets short capability by flipping one config field, zero
  main.py changes required.
- **Stop-out** lives in `template_repo/main.py`'s tick loop, not inside `decide()`,
  because it has to fire on ticks where `decide()` returns nothing — a price moving hard
  against an open short can't wait for the next vote.
- **Live**: no protocol, no margin. The "borrow" is real XLM deposited into claudio's
  wallet by hand ahead of time. A short-sell spends into that pre-funded buffer instead
  of ordinary trading capital; a cover buy replenishes it. `stellar_trader.py` treats it
  as a distinct, non-bypassable reserve so an ordinary sell can never eat into it by
  accident.

## 1. `tools/portfolio.py`

- `normalize_state()`: add `state['borrowed_xlm'] = _as_float(state.get('borrowed_xlm'))`,
  same pattern as `balance_usd`. Never touches `positions`.
- New `MAX_BORROWED_XLM = 6000.0` module constant (paper-side, not config-driven — mirrors
  `MAX_EXTRA_ASSETS` being a hard module constant rather than revision-widenable).
- `add_amount` is untouched — stays a strict non-negative clamp on real positions. The
  borrowed portion never goes through it.

## 2. `tools/trade_logger.py`

- `execute_trade(...)` gets one new keyword: `allow_shorting=False`. Callers (`main.py`)
  pass `config.get('allow_shorting', False)`.
- **Sell branch**, only when `is_native and allow_shorting`:
  ```python
  held = get_amount(state, spec)
  borrowed = state.get('borrowed_xlm', 0.0)
  headroom = max(0.0, MAX_BORROWED_XLM - borrowed)
  available = held + headroom          # was just `held`
  actual_asset = min(requested_usd / fill, available)
  from_held = min(actual_asset, held)
  from_borrow = actual_asset - from_held
  add_amount(state, spec, -from_held)
  state['borrowed_xlm'] = borrowed + from_borrow
  if from_borrow > 0:
      state['short_proceeds_usd'] = state.get('short_proceeds_usd', 0.0) + from_borrow * fill
  ```
- **Buy branch**: repay the borrow before growing the real position —
  ```python
  amount_asset = actual_usd / fill
  borrowed = state.get('borrowed_xlm', 0.0)
  repay = min(amount_asset, borrowed)
  state['borrowed_xlm'] = borrowed - repay
  if repay > 0:
      state['short_proceeds_usd'] = max(0.0, state.get('short_proceeds_usd', 0.0) - repay * fill)
  amount_asset -= repay
  if amount_asset > 0:
      add_amount(state, spec, amount_asset)
  ```
- **Live submission**: pass a new `short=(from_borrow > 0)` flag through to
  `submit_trade(side, trade_usd, asset=spec, short=...)`. Log line / `live` object
  unchanged otherwise — `short` just needs to land in the log for `live_report.py` to
  eventually distinguish.
- `short_proceeds_usd` is a simple running-average cost basis, same fidelity level
  `backtest.py` already uses for the long side (`cost_basis / held`) — good enough for a
  stop-out ratio, not meant to be lot-accurate.

## 3. `tools/backtest.py`

- Mirror steps 1-2 exactly in `backtest()`'s inline replay loop (it currently hard-clamps
  sells to `state['balance_xlm']`), so a backtested return stays comparable to live paper
  — an existing, explicit contract in this file's docstring, not new work invented here.
- Add `borrowed_xlm` / `short_proceeds_usd` to `_fresh_state()`.

## 4. `master_agent/score.py`

- `compute_score_multi`: after the existing positions loop, subtract the short liability:
  ```python
  borrowed = state.get('borrowed_xlm', 0.0)
  if borrowed > 0:
      mark = marks.get('XLM')
      if mark is not None:
          total -= borrowed * mark_price(mark) / UNREALIZED_HAIRCUT   # buy-back costs slightly *more*, not less
      else:
          unpriced.append('XLM:short-liability')   # can't mark it -> don't silently ignore a real liability
  ```
  Deliberately does **not** use `ILLIQUID_HAIRCUT` — that's calibrated as a mark-realism
  discount on thin non-XLM books, a different kind of number per that file's own
  commentary.

## 5. `template_repo/main.py`

- `config.json` gains `"allow_shorting": false` (default off, opt-in per strategy).
- `main()`'s tick loop, right after the price fetch and before calling `decide()`:
  ```python
  borrowed = state.get('borrowed_xlm', 0.0)
  if borrowed > 0:
      proceeds = state.get('short_proceeds_usd', 0.0)
      buyback_cost = borrowed * price
      if buyback_cost > proceeds * SHORT_STOP_OUT_RATIO:
          state = execute_trade(agent_name, 'cover_stoploss', 'buy', price, buyback_cost, state, allow_shorting=True)
  ```
  `SHORT_STOP_OUT_RATIO` (e.g. `1.5` — buy-back cost 50% above what was received forces a
  cover) as a module constant next to `TICK_SECONDS` / `MAX_HISTORY`.
- Pass `allow_shorting=config.get('allow_shorting', False)` on every `execute_trade` call
  for the XLM leg (not `decide_asset`'s non-XLM legs — shorting is XLM-only).

## 6. `tools/stellar_trader.py`

- New constant: `SHORT_BUFFER_XLM = 60.0`.
- `_sellable_xlm()` (ordinary sells + wind_down) floors at
  `MIN_TRUSTLINE_RESERVE_XLM + SHORT_BUFFER_XLM` whenever a buffer has been funded (see
  marker below), so a normal sell can never silently spend it.
- New `_short_sellable_xlm(account)` =
  `max(0, _spendable_xlm(account) - MIN_TRUSTLINE_RESERVE_XLM)` — the buffer itself, used
  only for short-sells.
- `submit_trade(side, usd_amount, *, asset='XLM', short=False)`: when
  `short and side == 'sell' and native`, use `_short_sellable_xlm` instead of
  `_sellable_xlm` for `available_usd`, and independently re-verify the buffer is actually
  funded rather than trusting the caller's `short=True` — same "request, not instruction"
  pattern already used for `asset`.
- Buffer funding tracked by a marker file, `/opt/trades/.short_buffer.json`, written once
  by hand after the real deposit (`{"funded_xlm": 60.0, "funded_at": ...}`). `submit_trade`
  fails closed if this file is absent — a short-sell request with no recorded buffer is
  refused, never silently allowed through.

## 7. `master_agent/domain_sdex.py` — the hard gate

- Extend `can_execute_live(name)`: after the existing AST check, if the strategy's
  `config.json` has `allow_shorting: true`, additionally require `.short_buffer.json` to
  exist **and** a live Horizon balance check confirming claudio's spendable XLM still
  covers `MIN_TRUSTLINE_RESERVE_XLM + SHORT_BUFFER_XLM` right now. Fails closed on any
  read/network error, consistent with this function's existing behavior.
- This is what makes it non-bypassable by `--promote NAME --force` — `--force` only skips
  `qualifies_for_live`'s track-record bar, never `can_execute_live`, so this lands in
  exactly the right place to stay a hard gate.

## 8. Operational steps (manual, real money)

1. Deposit 60 XLM into claudio's account beyond its existing reserve.
2. Write `.short_buffer.json` confirming the deposit.
3. Set `allow_shorting: true` in the target strategy's `config.json`.
4. `./monitor.py --promote NAME` (or `--force` if it hasn't hit the paper track-record bar
   yet) — `can_execute_live` refuses it if the buffer isn't actually there.

## 9. Testing order

1. `master_agent/selftest_domain.py` after touching `domain_sdex.py` (required by this
   repo's own rules).
2. Unit-level: hand-craft a `state.json` with `borrowed_xlm > 0`, run it through
   `backtest.py`'s loop logic directly, confirm P&L sign and stop-out firing on a
   synthetic price spike.
3. Backtest a shorting-enabled clone over the same 30-day window already established as
   baseline for `seed_1124713bc960` — the first real check of whether the mechanism does
   what's wanted, since that window's -15% XLM slide is exactly the scenario a short
   should profit from.
4. Smoke-test (`main_py_is_sane`'s `PAPER_ONLY=1` throwaway run) on a template clone with
   `allow_shorting: true` before ever promoting one live.
5. Manual REPL exercise of the new `stellar_trader.submit_trade(..., short=True)` path
   against the buffer, per this file's existing "exercise any change manually first"
   caution, before it's wired into a running strategy.

## Scope note

Roughly 8 files, ~150-200 lines of net new logic, all inside the boundary
`check_boundary_integrity` watches (`tools/`, `master_agent/`, `template_repo/`) — needs
to land as one commit per repo, not incrementally, or live trading halts partway through.

Recommended split: build and validate the paper-trading half first (`portfolio.py`,
`trade_logger.py`, `backtest.py`, `score.py`, `template_repo/main.py`) since it's fully
testable without touching real money; hold `stellar_trader.py` / `domain_sdex.py` (the
live half) until the paper mechanism is validated against the backtest baseline.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A sandbox for experimenting with tool-calling agents against a local Ollama
server, using the `gpt-oss:120b-cloud` cloud-backed model. There is no build system,
test suite, or linter.

## Docker

The main system is designed to run in a docker container created by `create.sh`.
There should be only one container running against any given `v/` directory.
The name of the container assigned to the current worktree is stored in
`.containername`.

## Setup and running

Dependencies live in `venv` (already created; versions pinned in `requirements.txt`:
`ollama`, `httpx`, `pydantic`, `requests`). Activate it before running scripts:

```
source venv/bin/activate
```

Each script needs `OLLAMA_API_KEY` set and expects an Ollama server reachable at the
`host` hardcoded in that script. Source the key first:

```
source env.sh
python emperor-agent.py
```

- `hello.py` — minimal one-shot streaming chat, no tools. Points at `127.0.0.1:11434`.
- `emperor-agent.py` — the actual tool-calling agent: an interactive REPL
  (`main()` reads from stdin) that loops `client.chat(...)` with `tools=TOOL_SCHEMAS`
  until the model responds without a tool call (`run_turn`). Also points at
  `127.0.0.1:11434`.

`create.sh` builds a container **and the `v/` volume it runs against**, for one domain:

```
./create.sh [--reuse-volume] [<domain>]        # domain defaults to sdex
docker start $(cat .containername)
```

Those two lines are the whole bootstrap. `v/` is gitignored, so a new worktree starts with
no volume at all, and everything the running system needs is built by `create.sh`: the
directory skeleton, a `./copy.sh --to` deploy, `v/env.sh` (with the key and `DOMAIN=`),
`git init` + first commit in `v/master_agent`, `v/tools` and every deployed
`template_repo*` (not just the selected domain's — `st.sh` walks them all, and switching
`DOMAIN` in `v/env.sh` later would otherwise land on a template that cannot be cloned),
the `domain-baseline` branch `selftest.sh` needs, and `/opt/agents/venv` built by the
*container's* interpreter. The container is left **stopped**; the emperor is held inert
during setup by `v/emperor.sh.UNMANAGED`, which is removed only after the stop.

Which template repo a domain seeds from is read out of `master_agent/domain_<name>.py`'s
`TEMPLATE_REPO` rather than kept in a table — the mapping is not mechanical
(`DOMAIN=sdex_maker` clones `/opt/template_repo_maker`), and the container re-answers the
same question from the imported module at the end so the two are compared. Every
registered domain has a template in this repo, so all five bootstrap; a domain whose
template is missing is refused by name rather than half-built.

`./create.sh null` is the cheap end-to-end test of the whole machine: no money, no
network, no API rate limits, and a game that resolves every few seconds instead of every
few hours. Use it to check that a change to `monitor.py` or the loop still scores, ranks,
culls, clones, revises and gates — then run the real domain.

It also runs *fast*, because `domain_null.py` declares a `PACING` dict (`domain.py`'s
group 8) that `create.sh` appends to `v/env.sh` as `export` lines: **a 120s monitor cycle
and a 5m emperor window**, against the image's 8h and 12h. `emperor.sh` sources `env.sh`
before it reads `EMPEROR_RUN_HOURS` or launches `monitor.py`, so those exports beat
supervisor's `environment=` line for both processes. `EMPEROR_RUN_HOURS` is a *window*,
not a period — the self-revision LLM call runs after it — so five minutes is the floor of
an emperor cycle, not its length. Two other settings ride along for the same reason:
`IDLE_GRACE_S` (3h would be 90 cycles here) and `EMPEROR_LOG_RETENTION` (prunes by cycle
count, so 48 is under four hours at this window). `domain_null.RANK_GRACE_S` is scaled the
same way, in the module itself, since it is already a domain-owned constant.

Every other domain declares no `PACING` and runs at the image's cadence, unchanged. The
dict is read off the domain module's *source* by `ast.literal_eval` on the host — never
imported, so `create.sh` cannot fail on a dependency — and its values are validated by
`domain.check_pacing` (which `domain.check` also calls) because they are written into a
file `emperor.sh` sources as root. The exports are written **by `create.sh` only**:
`copy.sh` never touches `v/env.sh`, so an existing volume keeps the cadence it was created
with until you re-run `create.sh` or add the lines by hand.

`v/agents/` holds a working copy of the agent scripts for execution/testing inside that
container. `v/` has since grown well beyond a script mirror into a separate multi-repo
trading-agent system (`master_agent/`, `template_repo/`, `tools/`, `strategies/`,
`trades/`); see `v/CLAUDE.md` for that.

`copy.sh` moves files between the two, and the direction depends on the flag:

- `./copy.sh --to` — root → `v/agents/`: copies `requirements.txt`,
  `emperor-agent.py`, `sr_agent_tools.py` and `tools.json` in, backing up the previous
  copy into `v/agents/bak.<random>/` first (skipped entirely on a fresh volume, where
  there is nothing to back up). It also copies `master_agent/*`, `tools/*`, every
  `template_repo*/*` in `SYNCED_DIRS` and `scripts/*.sh` across — with **flat globs**, so a
  new file must be flat inside those directories or it will silently not deploy. A new
  template repo has to be added to `SYNCED_DIRS`; `create.sh` refuses to bootstrap a domain
  whose template is missing from it, because that volume would freeze at creation time.
- `./copy.sh` (no args) — `v/` → root: copies `v/agents/*` to the repo root and
  `v/{master_agent,tools,template_repo}/*` over their root-level counterparts.

The top-level `master_agent/`, `template_repo/`, and `tools/` directories in *this* repo
(not `v/`) are the git-tracked mirror maintained by that no-arg direction, so their
history is the backup/reference record of what the live system has done to itself. The
sync is manual, though, so don't assume they're current unless `copy.sh` has been run
recently — and don't edit the root-level copies expecting it to affect a running system.
The live versions are the ones under `v/`, separate repos that get rewritten by the
self-modifying agent while the container runs.

## Architecture: the domain boundary (master_agent/)

`master_agent/monitor.py` is the domain-agnostic evolutionary loop: score, rank, cull,
retire, clone, revise, gate, promote, sleep. It knows nothing about what is being traded.
Everything that *is* about a specific way of making money sits behind one module:

- `master_agent/domain.py` — the contract, plus the `DOMAIN` env-var registry
  (`domain.get()`), a `check(mod)` validator, and the shared filesystem/role constants.
  Its module docstring is the spec: read it before adding anything to `monitor.py`.
  It also owns `live_switch()`, the domain-agnostic off switch for real execution
  (`/opt/.live_disabled`, or `LIVE_TRADING=off` on the monitor process), which every
  domain's `live_enabled()` contract member defers to before adding its own reasons.
  `live_enabled()` gates promotion and the live flag for the whole domain;
  `can_execute_live(name)` is the per-strategy question and neither replaces the other.
  Both fail closed. `monitor.py --live-status` prints the verdict.
- `master_agent/domain_sdex.py` — the Stellar DEX domain (the default). Prices, marks,
  order books, assets, threshold bands, the config gates, the seed/tweak fallbacks, the
  promotion gates, the real-money caps.
- `master_agent/domain_null.py` — a coin-flip forecasting game with no prices and no
  money. Selected with `DOMAIN=null`. It exists to prove the contract carries a domain
  that has none of sdex's furniture, and was the skeleton for the real benchmark domain
  below. It is also the domain to reach for when testing the loop itself: `./create.sh
  null` brings the whole system up — emperor loop, revision cycles and all — against a
  game that costs nothing and resolves in seconds, at the 2-minute/5-minute cadence its
  `PACING` dict asks for (above).

  Its seed genome is `template_repo_null/`. A question shows one number, `feature` in
  [0, 1], which is the true probability it resolves True; `decide()` answers True, False
  or None to skip, and the outcome is drawn only *after* decide() returns, from a number
  decide() never received. A right answer pays +1, a wrong one -1, and every answer is
  charged `ANSWER_COST` — that charge is what makes it a game rather than a counter,
  since without it answering is always non-negative and the only strategy is to answer
  everything. The template's rule answers when `|feature - 0.5| >= confidence - 0.5`, so
  `confidence` is a selectivity threshold whose steady-state payoff `(2 - 2c) * (c -
  0.25)` peaks at **c = 0.625** — an interior optimum, inside the range
  `seed_config` seeds from and reachable by `tweak_config`'s ±5% jitter, so there is
  something real for the loop to find. `points` is a rolling *time* window, not a
  lifetime total: a total would make score a function of age and rank-culling would
  measure birthdays, while a per-answer average would reward answering almost nothing.

  `domain_null.score()` reads that number out of the strategy's own `state.json` and
  nothing audits it — that is deliberate and is what keeps this a skeleton rather than a
  benchmark (`domain_forecast.py` is the judged one). The revision prompt says so
  explicitly, because a model that notices can "win" by writing a big number into
  state.json, which measures nothing.
- `master_agent/domain_forecast.py` — the benchmark domain FUTURE.md item 3 asked for:
  free, fast, no money, and *actually* scored (unlike `domain_null`, where a strategy
  just writes a number into its own `state.json` and nothing checks it). Selected with
  `DOMAIN=forecast`; the mechanics (question generation, resolution, Brier scoring) live
  in `tools/forecast_engine.py`, with `tools/forecast_backtest.py` as its offline replay
  and `template_repo_forecast` as its clone source. See `v/CLAUDE.md` for the detail —
  this is a new module, not `domain_null.py` edited in place, because
  `selftest_domain.py`'s differential tests pin `domain_null.py`'s exact behaviour as the
  minimal contract-conformance reference.
- `master_agent/selftest_domain.py` — run this after touching any of the above. It loads
  the pre-refactor `monitor.py` out of git and requires the new domain members to give the
  same answers, so a behaviour change cannot pass unnoticed.

**Run it in the container**, against the deployed code:

```
./copy.sh --to
docker exec $(cat .containername) /opt/selftest.sh
```

`scripts/selftest.sh` is the deployed entry point (`copy.sh --to` puts it at
`/opt/selftest.sh`, beside `once.sh` and `st.sh`). The container is the only place worth
trusting the result: only there do the real order books, the recorded basis distribution,
`regime.suggest_bands`' band grid and the live population get exercised. On a host with no
`/opt/tools`, a dozen paths quietly take their degraded branch and the run proves much less.

It checks the container as well as the refactor — three environmental faults that stay
invisible until they cost an emperor window: an interpreter that cannot `import ollama`, a
watched repo that is already dirty, and, the common one on a fresh container, a **missing
`.gitignore` in `/opt/tools` or `/opt/master_agent`**. Both directions of `copy.sh` use
`cp dir/*`, which does not match dotfiles, so those files never sync; without them the first
cache write or `__pycache__` leaves an untracked file and the next cycle prints `LIVE
TRADING HALTED`. The script prints the exact fix and deliberately does not apply it — a
self-test that edits the real-money boundary is not a self-test.

It copies `/opt/tools` to a scratch path first on `PYTHONPATH` and sets
`PYTHONDONTWRITEBYTECODE`, so running the test cannot itself dirty a watched repo, and it
diffs both watched repos before against after to prove it. Log:
`/opt/emperor_logs/selftest-<stamp>.log`, which is `v/emperor_logs/` on the host.

The baseline is found by content, not configured: it searches `/opt/master_agent`'s own
`monitor.py` history for the last commit that still defined `_config_is_sane`,
`fetch_marks_for_cycle` and `apply_seed_thresholds`. That repo is written by `emperor.sh`'s
self-revision passes, so the commit id differs from the mirror's and no constant could name
both. Not finding one is a **failure**, not a skip — a green run that verified nothing is
worse than a red one.

Rule of thumb: if you are about to write `price`, an asset code, a threshold or a trade
into `monitor.py`, it belongs in a domain module instead. The per-cycle `obs` the loop
threads around is deliberately opaque — the loop only tests it for `None`.

## Architecture: tool-calling loop

The agent pattern split across `emperor-agent.py`, `sr_agent_tools.py`, and
`tools.json` is:

1. **`tools.json`** — OpenAI-style tool schemas (`type: function`, JSON-schema
   `parameters`) passed to `client.chat(..., tools=TOOL_SCHEMAS)`. This is the
   source of truth for what the model is told is callable.
2. **`TOOLS` dict** in `emperor-agent.py` — maps each schema's `name` to the actual
   Python callable. Tool implementations can live inline in `emperor-agent.py`
   (e.g. `calculate`, `get_current_time`) or be imported from a separate module like
   `sr_agent_tools.py` (e.g. `get_uptime`).
3. **`dispatch()`** — takes one `tool_call` from the model's response, looks it up in
   `TOOLS`, calls it with the model-supplied kwargs, and returns a `{'role': 'tool',
   ...}` message.
4. **`run_turn()`** — the agent loop: send messages to the model, append its reply,
   and if it requested tool calls, dispatch each one, append the results, and go
   around again. Returns once the model replies with plain content instead of tool
   calls.

When adding a new tool: add its implementation (in `emperor-agent.py` or a new
function in `sr_agent_tools.py`), register it in the `TOOLS` dict, and add a matching
schema entry to `tools.json`. The schema `description` is what the model uses to
decide when to call the tool, so be specific about when it applies (see the
`get_uptime` schema for the level of detail expected).

`calculate` evaluates arithmetic via a restricted `ast` walk (`_eval_node`/`_OPS`)
rather than `eval()` — only `Constant`, `BinOp`, and `UnaryOp` nodes with the
operators in `_OPS` (`+ - * / **` and unary minus) are permitted.

## Secrets

`env.sh` (and `v/agents/env.sh`) contain a live `OLLAMA_API_KEY` and are gitignored.
Don't commit them or echo their contents into tracked files.

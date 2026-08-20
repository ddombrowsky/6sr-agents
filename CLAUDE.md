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

`create.sh` starts a detached `ubuntu` container with the `v/` directory bind-mounted to
`/opt`. `v/` is gitignored; `v/agents/` holds a working copy of the agent scripts for
execution/testing inside that container. `v/` has since grown well beyond a script mirror
into a separate multi-repo trading-agent system (`master_agent/`, `template_repo/`,
`tools/`, `strategies/`, `trades/`); see `v/CLAUDE.md` for that.

`copy.sh` moves files between the two, and the direction depends on the flag:

- `./copy.sh --to` — root → `v/agents/`: copies `requirements.txt`,
  `emperor-agent.py`, `sr_agent_tools.py` and `tools.json` in, backing up the previous
  copy into `v/agents/bak.<random>/` first. It also copies `master_agent/*`, `tools/*`,
  `template_repo/*` and `scripts/*.sh` across — with **flat globs**, so a new file must be
  flat inside those directories or it will silently not deploy.
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
- `master_agent/domain_sdex.py` — the Stellar DEX domain (the default). Prices, marks,
  order books, assets, threshold bands, the config gates, the seed/tweak fallbacks, the
  promotion gates, the real-money caps.
- `master_agent/domain_null.py` — a coin-flip forecasting game with no prices and no
  money. Selected with `DOMAIN=null`. It exists to prove the contract carries a domain
  that has none of sdex's furniture, and was the skeleton for the real benchmark domain
  below.
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

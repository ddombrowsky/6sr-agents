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
python agent-bootstrap.py
```

- `hello.py` — minimal one-shot streaming chat, no tools. Points at `127.0.0.1:11434`.
- `agent-bootstrap.py` — the actual tool-calling agent: an interactive REPL
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
  `agent-bootstrap.py`, `sr_agent_tools.py` and `tools.json` in, backing up the previous
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
  that has none of sdex's furniture, and is the skeleton for the benchmark domain in
  FUTURE.md's plan.
- `master_agent/selftest_domain.py` — run this after touching any of the above. It loads
  the pre-refactor `monitor.py` out of git and requires the new domain members to give the
  same answers, so a behaviour change cannot pass unnoticed.

Two ways to run it:

```
python3 master_agent/selftest_domain.py     # on the host: /opt/tools is absent, so the
                                            # asset/basis/replay paths take their degraded
                                            # branch (on both sides of the differential)
./run-selftest-in-container.sh              # in the container, against the real /opt/tools
                                            # and the real population. Deploys nothing.
```

Prefer the container run before letting a cycle go — it is the only one that exercises the
real order books, the recorded basis distribution, `regime.suggest_bands`' band grid and
the live population. Read the header of `run-selftest-in-container.sh` for why it copies
the repo and `/opt/tools` to container-local paths instead of using `copy.sh --to`: running
from `/opt` would silently skip every differential check (different git repo) and would
dirty the repos `check_boundary_integrity()` halts live trading on. Logs land in
`selftest-logs/` (gitignored) and in the container under `/root/domain-selftest-logs/`.

Rule of thumb: if you are about to write `price`, an asset code, a threshold or a trade
into `monitor.py`, it belongs in a domain module instead. The per-cycle `obs` the loop
threads around is deliberately opaque — the loop only tests it for `None`.

## Architecture: tool-calling loop

The agent pattern split across `agent-bootstrap.py`, `sr_agent_tools.py`, and
`tools.json` is:

1. **`tools.json`** — OpenAI-style tool schemas (`type: function`, JSON-schema
   `parameters`) passed to `client.chat(..., tools=TOOL_SCHEMAS)`. This is the
   source of truth for what the model is told is callable.
2. **`TOOLS` dict** in `agent-bootstrap.py` — maps each schema's `name` to the actual
   Python callable. Tool implementations can live inline in `agent-bootstrap.py`
   (e.g. `calculate`, `get_current_time`) or be imported from a separate module like
   `sr_agent_tools.py` (e.g. `get_uptime`).
3. **`dispatch()`** — takes one `tool_call` from the model's response, looks it up in
   `TOOLS`, calls it with the model-supplied kwargs, and returns a `{'role': 'tool',
   ...}` message.
4. **`run_turn()`** — the agent loop: send messages to the model, append its reply,
   and if it requested tool calls, dispatch each one, append the results, and go
   around again. Returns once the model replies with plain content instead of tool
   calls.

When adding a new tool: add its implementation (in `agent-bootstrap.py` or a new
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

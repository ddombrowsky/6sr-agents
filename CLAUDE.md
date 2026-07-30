# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small sandbox for experimenting with tool-calling agents against a local Ollama
server, using the `gpt-oss:120b-cloud` cloud-backed model. There is no build system,
test suite, or linter — it's a handful of standalone scripts.

## Setup and running

Dependencies live in `venv` (already created; no `requirements.txt` — installed
packages are `ollama`, `httpx`, `pydantic`, etc.). Activate it before running scripts:

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

`create.sh` starts a detached `ubuntu` container (`docker run -d --name agenttest
--volume ./v:/opt ubuntu sleep infinity`) with the `v/` directory bind-mounted to
`/opt`. `v/` is gitignored; `v/agents/` holds a working copy of the agent scripts for
execution/testing inside that container, refreshed from the root copies by `copy.sh`
(which backs up the previous copy into `v/agents/bak.<random>/` first) — don't assume
it's in sync unless `copy.sh` has been run recently. `v/` has since grown well beyond
a script mirror into a separate multi-repo trading-agent system (`master_agent/`,
`template_repo/`, `tools/`, `trades/`); see `v/CLAUDE.md` for that.

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

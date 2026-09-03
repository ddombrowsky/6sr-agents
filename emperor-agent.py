import json
import os
import re
import sys
import time

from ollama import Client, ResponseError

SELF_FILE = os.path.abspath(__file__)
SELF_DIR = os.path.dirname(SELF_FILE)

# WHERE THIS AGENT'S TOOLS COME FROM, and why it is not just `import sr_agent_tools`.
#
# There are two sr_agent_tools.py / tools.json pairs in a running container:
#
#   /opt/agents/       -- this directory. A frozen `copy.sh --to` deploy of the host
#                         repo's root-level copies. Nothing in the container ever writes
#                         to it.
#   /opt/master_agent/ -- its own git repo, and the one emperor.sh's prompt names in
#                         step 1. Every emperor pass reads and revises it.
#
# So this agent spent eleven passes hardening the tools it does not use. By 2026-09-03 the
# gap was 13 tools against 23, and the three it was missing were precisely the ones that
# make a large-codebase review affordable:
#
#   * read_file's line_start/line_end paging. Without it the only way to see a file is
#     whole, and the four files emperor.sh asks for are 5,720 lines / 327KB ~= 81k tokens
#     of a 131k window. Step 1 of the prompt, obeyed literally, spends 62% of the context
#     before the agent has thought about anything.
#   * a working apply_patch. /opt/agents/sr_agent_tools.py has the wrapper but not the
#     `sys.path.append('/opt/tools')` that lets it find /opt/tools/apply_patch.py, so
#     every call returned "apply_patch module not available" and the model fell back to
#     re-reading the file whole so it could rewrite it whole.
#   * search (grep -rn), so a targeted question does not need a file read at all.
#
# The three runs before this was found (2026-09-01, -09-02, -09-03) changed nothing at
# all: one died on `The prompt is too long: 145759, model maximum context length: 131072`,
# one emitted a malformed tool call and exited, and one burned its window on the read /
# failed-patch / re-read loop above. Two of those three are directly this.
#
# Overridable so the root-level copies still run standalone on a host with no /opt, and
# so a bisect can pin the old behaviour with EMPEROR_TOOL_STACK=/opt/agents.
TOOL_STACK_DIR = os.environ.get('EMPEROR_TOOL_STACK') or '/opt/master_agent'
if all(os.path.isfile(os.path.join(TOOL_STACK_DIR, f))
       for f in ('sr_agent_tools.py', 'tools.json')):
    # Ahead of SELF_DIR, which is sys.path[0] for a script. memory_tools.py exists only
    # in SELF_DIR, so put that back explicitly rather than trusting the interpreter to
    # have added it: `python emperor-agent.py` does, but runpy.run_path() and `python -m`
    # do not, and the failure mode is an import error at line 1 of a 12-hour cycle.
    sys.path.insert(0, TOOL_STACK_DIR)
    if SELF_DIR not in sys.path:
        sys.path.append(SELF_DIR)
else:
    print(f'[warning] no tool stack at {TOOL_STACK_DIR}; falling back to {SELF_DIR}. '
          'Expect no read_file paging, no search, and a broken apply_patch.')
    TOOL_STACK_DIR = SELF_DIR

TOOLS_FILE = os.environ.get('EMPEROR_TOOLS_FILE') or os.path.join(TOOL_STACK_DIR, 'tools.json')

# Deliberately below the sys.path work above: which sr_agent_tools this resolves to is the
# entire point of this block, and moving these back up to the other imports silently
# reinstates the bug.
import memory_tools  # noqa: E402
import sr_agent_tools  # noqa: E402

MODEL_NICKNAMES = {
    'gpt': 'gpt-oss:120b-cloud',
    'glm': 'glm-5.2:cloud',
    'qwen': 'qwen3.5',
    'buck': 'wonderful_buck_321/sixsr',
    'granite': 'granite4.1:8b',
}
MODEL = MODEL_NICKNAMES['gpt']


def _is_cloud_model(model: str) -> bool:
    """Is `model` one of Ollama's cloud-hosted models rather than a local pull?

    Ollama names them by tag, in two shapes that both appear in MODEL_NICKNAMES:
    a bare `:cloud` (glm-5.2:cloud) and a sized `:<size>-cloud` (gpt-oss:120b-cloud).
    Match on the tag rather than searching the whole string so a local model that merely
    has 'cloud' in its name -- a user pull like `someone/cloudy:8b` -- is not swept in.

    Kept identical to master-agent.py's copy. The two files cannot share it: that one is
    named with a hyphen and is not importable, which is the same reason the ROLE_*
    constants had to move into domain.py.
    """
    tag = model.rsplit(':', 1)[-1] if ':' in model else ''
    return tag == 'cloud' or tag.endswith('-cloud')


def _should_think() -> bool:
    """Whether to ask for a reasoning pass on this turn.

    On for cloud models, off for local ones. The cloud models here are all
    hybrid-reasoning and are trained to plan before a tool call; the local fallbacks are
    small (granite4.1:8b) and either do not reason at all or cannot afford the tokens on
    this host, so asking costs latency for nothing.

    With think=False a hybrid model does not stop reasoning -- it relocates it, emitting
    the chain of thought into `content` next to its tool_calls. That is the field
    emperor.sh's caller reads as the agent's answer, so reasoning landing there is noise
    in a load-bearing channel. think=True moves it to its own `thinking` key.

    Read at call time, not at import: MODEL is rebindable by `/model <nick>` in the REPL,
    and a switch to a local fallback has to turn thinking back off. Force it either way
    with AGENT_THINK=on|off when bisecting a model's behaviour.
    """
    override = os.environ.get('AGENT_THINK', '').strip().lower()
    if override in ('on', '1', 'true', 'yes'):
        return True
    if override in ('off', '0', 'false', 'no'):
        return False
    return _is_cloud_model(MODEL)
client = Client(
    host="http://172.17.0.1:11434",
    headers={'Authorization': 'Bearer ' + os.environ.get('OLLAMA_API_KEY')}
)

TOOLS = sr_agent_tools.TOOLS

with open(TOOLS_FILE) as f:
    TOOL_SCHEMAS = json.load(f)

# Printed on every run, into emperor_logs/agent_<stamp>.log. The mismatch above was
# invisible for eleven passes because nothing ever said which stack was loaded -- the
# logs of a crippled run and a healthy one were identical up to the first tool error.
# A schema the model is offered but that maps to no callable is worth naming too:
# `remember` was in the TOOLS dict and in neither tools.json for the life of this agent,
# so the system prompt's closing instruction to "use the remember tool" named something
# the model was never told existed.
print(f'[info] tool stack: {os.path.abspath(sr_agent_tools.__file__)} '
      f'({len(TOOLS)} callables), schemas: {TOOLS_FILE} ({len(TOOL_SCHEMAS)})')
_schema_names = {t['function']['name'] for t in TOOL_SCHEMAS}
for _missing in sorted(_schema_names - set(TOOLS)):
    print(f'[warning] schema {_missing!r} has no implementation in this tool stack')
for _unoffered in sorted(set(TOOLS) - _schema_names):
    print(f'[warning] tool {_unoffered!r} is implemented but has no schema; '
          'the model will never call it')


def dispatch(tool_call) -> dict:
    name = tool_call['function']['name']
    args = tool_call['function']['arguments']
    print(f'  -> {name}({", ".join(f"{k}={v!r}" for k, v in args.items())})')
    fn = TOOLS.get(name)
    if not fn:
        result = f'error: unknown tool {name}'
    else:
        try:
            result = fn(**args)
        except Exception as e:
            result = f'error: {type(e).__name__}: {e}'
    result = str(result)
    shown = result if len(result) <= 200 else result[:200] + '...'
    print(f'  <- {shown}')
    return {'role': 'tool', 'name': name, 'content': result}


def _message_chars(msg) -> int:
    """Size of one message, counting reasoning and tool calls as well as `content`.

    With _should_think() on, an assistant turn's `thinking` can outweigh its `content`,
    and it is sent back on the next turn like everything else. An assistant turn that
    requested tools carries the call names and their JSON arguments too, which for a
    tool like write_file is most of the message. Counting only `content` under-reads
    the prompt -- harmless in the log line below, not harmless in _truncate_messages,
    where it drops too few messages and spends another round trip rediscovering that
    the prompt is still too long.
    """
    chars = sum(len(str(msg.get(key) or '')) for key in ('content', 'thinking'))
    for call in msg.get('tool_calls') or ():
        fn = call.get('function') or {}
        chars += len(str(fn.get('name') or ''))
        chars += len(json.dumps(fn.get('arguments') or {}, default=str))
    return chars


# Measured against the server's own count: the turn in
# emperor_logs/agent_20260901_230829.log estimated ~119k tokens for a prompt the server
# scored at 145,759 -- about 3.3 chars/token, not the 4 this used to assume. Code and
# JSON tokenize denser than prose, and this agent's context is mostly read_file output.
_CHARS_PER_TOKEN = 3.3


# The server states an overflow in one of two shapes:
#   "... exceeded max context length by 4096 tokens"
#   "The prompt is too long: 145759, model maximum context length: 131072"
# Only the first names the overflow directly; the second gives both totals, so the delta
# has to be subtracted out. Recognising just one of them costs twice: _truncate_messages
# falls back to blindly halving the history, and -- because the gate in run_turn used the
# substring 'prompt too long', which "prompt *is* too long" does not contain -- the 400
# escaped run_turn entirely and killed the emperor window.
_OVERFLOW_RES = (
    re.compile(r'exceeded max context length by (\d+) tokens', re.I),
    re.compile(r'prompt is too long:\s*(\d+),\s*model maximum context length:\s*(\d+)', re.I),
)
_OVERFLOW_MARKERS = (
    'prompt too long',
    'prompt is too long',
    'exceeded max context length',
    'context length exceeded',
)


def _is_overflow_error(error_text: str) -> bool:
    """True if this ResponseError is the prompt outgrowing the context window."""
    lowered = error_text.lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


def _overflow_tokens(error_text: str):
    """How many tokens over the limit the prompt was, or None if the error doesn't say."""
    for pattern in _OVERFLOW_RES:
        match = pattern.search(error_text)
        if not match:
            continue
        groups = [int(g) for g in match.groups()]
        return groups[0] if len(groups) == 1 else groups[0] - groups[1]
    return None


def _truncate_messages(messages: list, error_text: str) -> bool:
    """Drop the oldest non-system messages to shrink the prompt.

    Keeps the system message (if any) and the most recent message (the one
    that triggered this turn) intact. Returns False if there's nothing left
    to drop.
    """
    keep_from = 1 if messages and messages[0].get('role') == 'system' else 0
    droppable = len(messages) - 1 - keep_from  # never drop the last message
    if droppable <= 0:
        return False

    overflow = _overflow_tokens(error_text)
    if overflow and overflow > 0:
        # Margin on top, since _CHARS_PER_TOKEN is still an average.
        target_chars = overflow * _CHARS_PER_TOKEN * 1.2
        removed_chars = 0
        removed = 0
        for msg in messages[keep_from:keep_from + droppable]:
            removed_chars += _message_chars(msg)
            removed += 1
            if removed_chars >= target_chars:
                break
    else:
        removed = max(1, droppable // 2)

    # Never leave behind a tool result whose requesting assistant message was just
    # dropped: an orphaned tool role is rejected outright, which would turn a
    # recoverable overflow into a hard failure on the very next retry.
    while removed < droppable and messages[keep_from + removed].get('role') == 'tool':
        removed += 1

    del messages[keep_from:keep_from + removed]
    print(f'[warning] prompt too long; dropped {removed} older message(s) and retrying')
    return True


SERVER_ERROR_MAX_RETRIES = 3
SERVER_ERROR_RETRY_DELAY = 2  # seconds


# TOOL_SCHEMAS rides along with every single request but lives in no message, so it has
# to be added back or the estimate is short by a constant few thousand tokens.
_TOOL_SCHEMA_TOKENS = int(len(json.dumps(TOOL_SCHEMAS)) / _CHARS_PER_TOKEN)


def _estimate_context_tokens(messages: list) -> int:
    """Rough token estimate of the whole prompt: the messages plus the tool schemas."""
    message_chars = sum(_message_chars(msg) for msg in messages)
    return int(message_chars / _CHARS_PER_TOKEN) + _TOOL_SCHEMA_TOKENS


def run_turn(messages: list) -> str:
    server_error_retries = 0
    while True:
        print('...')
        print(f'[info] estimated context size: ~{_estimate_context_tokens(messages)} tokens')
        try:
            response = client.chat(MODEL, messages=messages, tools=TOOL_SCHEMAS, think=_should_think())
        except ResponseError as e:
            error_text = str(e)
            if _is_overflow_error(error_text) and _truncate_messages(messages, error_text):
                server_error_retries = 0
                continue
            if e.status_code >= 500:
                server_error_retries += 1
                if server_error_retries > SERVER_ERROR_MAX_RETRIES:
                    print(f'[warning] server error persisted after {SERVER_ERROR_MAX_RETRIES} retries, giving up on this turn: {error_text}')
                    return f'[error: server returned "{error_text}" after {SERVER_ERROR_MAX_RETRIES} retries — try again]'
                print(f'[warning] server error ({error_text}); retrying in {SERVER_ERROR_RETRY_DELAY}s ({server_error_retries}/{SERVER_ERROR_MAX_RETRIES})...')
                time.sleep(SERVER_ERROR_RETRY_DELAY)
                continue
            raise
        server_error_retries = 0
        message = response['message']
        messages.append(message)

        tool_calls = message.get('tool_calls')
        if not tool_calls:
            return message['content']

        for call in tool_calls:
            messages.append(dispatch(call))


def _handle_model_command(user_input: str) -> bool:
    """If user_input is a `/model <nickname>` command, apply it and return True."""
    global MODEL
    parts = user_input.strip().split()
    if not parts or parts[0] != '/model':
        return False
    if len(parts) != 2:
        print(f'usage: /model <nickname>  (available: {", ".join(MODEL_NICKNAMES)})')
        return True
    nickname = parts[1]
    model = MODEL_NICKNAMES.get(nickname)
    if model is None:
        print(f'[error] unknown model nickname {nickname!r} (available: {", ".join(MODEL_NICKNAMES)})')
        return True
    MODEL = model
    print(f'[info] switched model to {nickname!r} ({MODEL})')
    return True


def _process_turn(messages: list, user_input: str) -> None:
    if not user_input.strip():
        return
    if _handle_model_command(user_input):
        return
    messages.append({'role': 'user', 'content': user_input})
    reply = run_turn(messages)
    print(reply)


def _read_piped_turn() -> str:
    """Collapse all of piped stdin into a single turn's content.

    emperor.sh (and similar callers) pipe a whole multi-line document as one
    coherent prompt, followed by a trailing `exit`/`quit` line so the REPL
    terminates afterward. Looping on input() would instead split that
    document into one turn per line, so non-interactive stdin is read in one
    shot here instead.
    """
    lines = sys.stdin.read().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip().lower() in ('exit', 'quit'):
        lines.pop()
    return '\n'.join(lines).strip()


def main():
    system_content = (
        'You are a top-level "emperor" agent. Your job is to investigate the system you are '
        'running on, install whatever tools you need to get things done, and extend '
        'your own capabilities by writing new agents and tools when the ones you have '
        "aren't enough.\n\n"
        'Start by understanding your environment: check uptime, read relevant files, '
        'and use apt to see what is and is not already installed before assuming a '
        'tool is missing. Use update_package_list before install_package if the '
        'package cannot be found.\n\n'
        'When a task calls for a capability you do not have, do not just say so — '
        'write it. Use write_file to add new tool implementations (in a Python module '
        'alongside your own) and update the tool schema so future turns can call them. '
        'Prefer small, single-purpose tools over one large script, and verify a new '
        'tool works before relying on it.\n\n'
        'Be transparent about what you install and write to disk — this is a sandbox, '
        'but treat package installs and file writes as real, auditable actions.\n\n'
        'You do not persist within this process across runs — each run starts fresh. '
        'When told to remember something for later, use the remember tool: it is '
        'automatically loaded into this system prompt on every future run, so you do '
        'not need to re-derive or re-read it from the filesystem yourself.'
    )
    memory_context = memory_tools.load_memory_context()
    if memory_context:
        system_content += '\n\n' + memory_context
    messages = [{'role': 'system', 'content': system_content}]
    print("Agent ready. Type 'exit' to quit.")

    if not sys.stdin.isatty():
        _process_turn(messages, _read_piped_turn())
        return

    while True:
        try:
            user_input = input('> ')
        except EOFError:
            break
        if user_input.strip().lower() in ('exit', 'quit'):
            break
        _process_turn(messages, user_input)


if __name__ == '__main__':
    main()

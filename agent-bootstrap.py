import json
import os
import re
import sys
import time

from ollama import Client, ResponseError

import sr_agent_tools

MODEL = 'gpt-oss:120b-cloud'
#MODEL = 'qwen3.5'
SELF_FILE = os.path.abspath(__file__)
TOOLS_FILE = os.path.join(os.path.dirname(SELF_FILE), 'tools.json')
TOOLS_MODULE_FILE = os.path.abspath(sr_agent_tools.__file__)
STATE_FILE = os.path.join(os.path.dirname(SELF_FILE), '.agent-state.json')

client = Client(
    host="http://172.17.0.1:11434",
    headers={'Authorization': 'Bearer ' + os.environ.get('OLLAMA_API_KEY')}
)

TOOLS = sr_agent_tools.TOOLS

with open(TOOLS_FILE) as f:
    TOOL_SCHEMAS = json.load(f)


def _watched_mtimes() -> dict:
    """mtimes of files that define tool implementations/schemas.

    Compared against a startup snapshot so a tool that edits its own
    definitions (e.g. via write_file) can trigger a re-exec that picks up
    the change instead of running stale in-memory code.
    """
    paths = [TOOLS_FILE, TOOLS_MODULE_FILE]
    return {p: os.path.getmtime(p) for p in paths if os.path.exists(p)}


_STARTUP_MTIMES = _watched_mtimes()


def _reexec_if_tools_changed(messages: list) -> None:
    if _watched_mtimes() == _STARTUP_MTIMES:
        return
    print('[info] tools.json or sr_agent_tools.py changed on disk; re-executing to reload them...')
    with open(STATE_FILE, 'w') as f:
        json.dump(messages, f)
    os.execv(sys.executable, [sys.executable, SELF_FILE])


def dispatch(tool_call) -> dict:
    name = tool_call['function']['name']
    args = tool_call['function']['arguments']
    print(f'  -> {name}({", ".join(f"{k}={v!r}" for k, v in args.items())})')
    fn = TOOLS.get(name)
    result = fn(**args) if fn else f'error: unknown tool {name}'
    result = str(result)
    shown = result if len(result) <= 200 else result[:200] + '...'
    print(f'  <- {shown}')
    return {'role': 'tool', 'name': name, 'content': result}


_OVERFLOW_RE = re.compile(r'exceeded max context length by (\d+) tokens')


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

    match = _OVERFLOW_RE.search(error_text)
    if match:
        # ~4 chars/token, with a margin since this is a rough estimate.
        target_chars = int(match.group(1)) * 4 * 1.2
        removed_chars = 0
        removed = 0
        for msg in messages[keep_from:keep_from + droppable]:
            removed_chars += len(str(msg.get('content') or ''))
            removed += 1
            if removed_chars >= target_chars:
                break
    else:
        removed = max(1, droppable // 2)

    del messages[keep_from:keep_from + removed]
    print(f'[warning] prompt too long; dropped {removed} older message(s) and retrying')
    return True


SERVER_ERROR_MAX_RETRIES = 3
SERVER_ERROR_RETRY_DELAY = 2  # seconds


def _estimate_context_tokens(messages: list) -> int:
    """Rough token estimate (~4 chars/token) of the current message list."""
    chars = sum(len(str(msg.get('content') or '')) for msg in messages)
    return chars // 4


def run_turn(messages: list) -> str:
    server_error_retries = 0
    while True:
        print('...')
        print(f'[info] estimated context size: ~{_estimate_context_tokens(messages)} tokens')
        try:
            response = client.chat(MODEL, messages=messages, tools=TOOL_SCHEMAS)
        except ResponseError as e:
            error_text = str(e)
            if 'prompt too long' in error_text.lower() and _truncate_messages(messages, error_text):
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
            _reexec_if_tools_changed(messages)


def main():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            messages = json.load(f)
        os.remove(STATE_FILE)
        print('[info] reloaded tools; conversation resumed.')
    else:
        messages = [{
            'role': 'system',
            'content': (
                'You are a bootstrapping agent. Your job is to investigate the system you are '
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
                'but treat package installs and file writes as real, auditable actions.'
            )
        }]
    print("Agent ready. Type 'exit' to quit.")
    continue_forever=False
    while True:
        if continue_forever:
            user_input = 'You are in control.  Continue.'
        else:
            user_input = input('> ')
        if not user_input.strip():
            continue
        if user_input.strip().lower() in ('exit', 'quit'):
            break
        if user_input.strip().lower() == 'continue_forever':
            user_input = 'continue task'
            continue_forever = True
        messages.append({'role': 'user', 'content': user_input})
        reply = run_turn(messages)
        print(reply)


if __name__ == '__main__':
    main()

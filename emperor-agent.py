import json
import os
import re
import sys
import time

from ollama import Client, ResponseError

import sr_agent_tools

MODEL_NICKNAMES = {
    'gpt': 'gpt-oss:120b-cloud',
    'qwen': 'qwen3.5',
    'buck': 'wonderful_buck_321/sixsr',
    'granite': 'granite4.1:8b',
}
MODEL = MODEL_NICKNAMES['qwen']
SELF_FILE = os.path.abspath(__file__)
TOOLS_FILE = os.path.join(os.path.dirname(SELF_FILE), 'tools.json')

client = Client(
    host="http://172.17.0.1:11434",
    headers={'Authorization': 'Bearer ' + os.environ.get('OLLAMA_API_KEY')}
)

TOOLS = sr_agent_tools.TOOLS

with open(TOOLS_FILE) as f:
    TOOL_SCHEMAS = json.load(f)


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
            response = client.chat(MODEL, messages=messages, tools=TOOL_SCHEMAS, think=False)
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

import ast
import json
import operator
import os
import re
from datetime import datetime

from ollama import Client, ResponseError

import sr_agent_tools

MODEL = 'gpt-oss:120b-cloud'
#MODEL = 'qwen3.5'
TOOLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools.json')

client = Client(
    host="http://172.17.0.1:11434",
    headers={'Authorization': 'Bearer ' + os.environ.get('OLLAMA_API_KEY')}
)

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}


def _eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f'unsupported expression: {ast.dump(node)}')


def calculate(expression: str) -> str:
    try:
        return str(_eval_node(ast.parse(expression, mode='eval').body))
    except Exception as e:
        return f'error: {e}'


def get_current_time() -> str:
    return datetime.now().isoformat()


TOOLS = {
    'calculate': calculate,
    'get_current_time': get_current_time,
    'get_uptime': sr_agent_tools.get_uptime,
    'read_file': sr_agent_tools.read_file,
    'write_file': sr_agent_tools.write_file,
    'fetch_url': sr_agent_tools.fetch_url,
    'install_package': sr_agent_tools.install_package,
    'update_package_list': sr_agent_tools.update_package_list,
}

with open(TOOLS_FILE) as f:
    TOOL_SCHEMAS = json.load(f)


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


def run_turn(messages: list) -> str:
    while True:
        print('...')
        try:
            response = client.chat(MODEL, messages=messages, tools=TOOL_SCHEMAS)
        except ResponseError as e:
            if 'prompt too long' in str(e).lower() and _truncate_messages(messages, str(e)):
                continue
            raise
        message = response['message']
        messages.append(message)

        tool_calls = message.get('tool_calls')
        if not tool_calls:
            return message['content']

        for call in tool_calls:
            messages.append(dispatch(call))


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
    while True:
        user_input = input('> ')
        if not user_input.strip():
            continue
        if user_input.strip().lower() in ('exit', 'quit'):
            break
        messages.append({'role': 'user', 'content': user_input})
        reply = run_turn(messages)
        print(reply)


if __name__ == '__main__':
    main()

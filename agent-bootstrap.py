import ast
import json
import operator
import os
from datetime import datetime

from ollama import Client

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
    fn = TOOLS.get(name)
    result = fn(**args) if fn else f'error: unknown tool {name}'
    return {'role': 'tool', 'name': name, 'content': str(result)}


def run_turn(messages: list) -> str:
    while True:
        response = client.chat(MODEL, messages=messages, tools=TOOL_SCHEMAS)
        message = response['message']
        messages.append(message)

        tool_calls = message.get('tool_calls')
        if not tool_calls:
            return message['content']

        for call in tool_calls:
            messages.append(dispatch(call))


def main():
    messages = [
        {'role': 'system', 'content': 'You are a helpful assistant with access to tools.'},
    ]
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

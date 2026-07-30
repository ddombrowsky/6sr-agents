import ast
import operator
import subprocess
from datetime import datetime


def get_uptime() -> str:
    result = subprocess.run(['uptime'], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except Exception as e:
        return f'error: {e}'


def write_file(path: str, content: str) -> str:
    try:
        with open(path, 'w') as f:
            f.write(content)
        return f'wrote {len(content)} bytes to {path}'
    except Exception as e:
        return f'error: {e}'


def fetch_url(url: str) -> str:
    try:
        result = subprocess.run(['curl', '-sL', url], capture_output=True, text=True, check=True)
        return result.stdout
    except Exception as e:
        return f'error: {e}'


def install_package(package: str) -> str:
    try:
        result = subprocess.run(['apt-get', 'install', '-y', package], capture_output=True, text=True, check=True)
        return result.stdout
    except Exception as e:
        return f'error: {e}'


def update_package_list() -> str:
    try:
        result = subprocess.run(['apt-get', 'update'], capture_output=True, text=True, check=True)
        return result.stdout
    except Exception as e:
        return f'error: {e}'


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


def exec(command: str) -> str:
    try:
        result = subprocess.run(
            ['/bin/sh', '-c', command],
            capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            output += f'\n[exit code {result.returncode}]'
        return output
    except Exception as e:
        return f'error: {e}'


TOOLS = {
    'calculate': calculate,
    'get_current_time': get_current_time,
    'get_uptime': get_uptime,
    'read_file': read_file,
    'write_file': write_file,
    'fetch_url': fetch_url,
    'install_package': install_package,
    'update_package_list': update_package_list,
    'exec': exec,
}

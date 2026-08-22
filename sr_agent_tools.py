import ast
import operator
import subprocess
from datetime import datetime

from memory_tools import remember, recall, forget, list_memories


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


def apply_patch(patch: str = '', input: str = None, patch_text: str = None) -> str:
    """Apply a V4A ("*** Begin Patch") patch. Thin wrapper over /opt/tools/apply_patch.py.

    The parser lives in /opt/tools rather than here because there are two copies of this
    module -- /opt/agents/sr_agent_tools.py for emperor-agent.py and
    /opt/master_agent/sr_agent_tools.py for master-agent.py -- and both already have
    /opt/tools on sys.path. One implementation, no drift between the two agents.

    `input` and `patch_text` are accepted as aliases for the same reason exec() accepts
    `cmd`: the models are trained on codex's apply_patch, whose schema names the argument
    `input`, and a TypeError over the argument name costs a tool-call round trip.
    """
    text = patch or input or patch_text or ''
    if not str(text).strip():
        return ('error: no patch provided -- pass the whole patch, "*** Begin Patch" '
                'through "*** End Patch", as the `patch` argument')
    try:
        import apply_patch as patcher
    except ImportError as e:
        return (f'error: apply_patch module not available ({e}) -- use write_file '
                'with the complete file contents instead')
    try:
        return patcher.apply_patch(str(text))
    except patcher.PatchError as e:
        return f'error: {e}'
    except Exception as e:
        return f'error: {type(e).__name__}: {e}'


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
    'apply_patch': apply_patch,
    'fetch_url': fetch_url,
    'install_package': install_package,
    'update_package_list': update_package_list,
    'exec': exec,
    'remember': remember,
    'recall': recall,
    'forget': forget,
    'list_memories': list_memories,
}

import json
import os

MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'memory.json')


def _load() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict) -> None:
    with open(MEMORY_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def remember(key: str, value: str) -> str:
    """Persist a fact under key so it survives into future agent runs."""
    try:
        data = _load()
        data[key] = value
        _save(data)
        return f'remembered {key!r}'
    except Exception as e:
        return f'error: {e}'


def recall(key: str) -> str:
    """Look up a previously remembered fact by key."""
    data = _load()
    if key not in data:
        return f'no memory found for {key!r}'
    return data[key]


def forget(key: str) -> str:
    """Delete a previously remembered fact by key."""
    data = _load()
    if key not in data:
        return f'no memory found for {key!r}'
    del data[key]
    _save(data)
    return f'forgot {key!r}'


def list_memories() -> str:
    """List all remembered keys and their values."""
    data = _load()
    if not data:
        return 'memory is empty'
    return '\n'.join(f'{k}: {v}' for k, v in data.items())


def load_memory_context() -> str:
    """Render all remembered facts as a block to fold into the system prompt.

    Returns '' when there's nothing stored, so callers can skip appending it.
    """
    data = _load()
    if not data:
        return ''
    lines = '\n'.join(f'- {k}: {v}' for k, v in data.items())
    return f'Persistent memory from previous runs:\n{lines}'

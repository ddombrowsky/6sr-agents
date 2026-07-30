import subprocess


def get_uptime() -> str:
    result = subprocess.run(['uptime'], capture_output=True, text=True, check=True)
    return result.stdout.strip()

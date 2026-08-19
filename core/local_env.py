#!/usr/bin/env python3
"""
Local development/runtime helpers.

These utilities are intentionally small and dependency-free. They load and
update the local .env file, discover VS Code Remote-SSH's SOCKS proxy, and
provide a few local port helpers for the launcher.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
PORT_SCAN_LIMIT = 40


def reexec_inside_venv(script_path: Path, argv: list[str]) -> None:
    """Re-execute a script with the project venv when available."""
    if not VENV_PYTHON.exists():
        return

    venv_root = ROOT / ".venv"
    try:
        already_in_venv = Path(sys.prefix).resolve() == venv_root.resolve()
    except OSError:
        already_in_venv = False

    if not already_in_venv:
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(script_path), *argv])


def load_dotenv(path: Path = ENV_PATH) -> dict[str, str]:
    """Load .env into os.environ without overriding existing environment values."""
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def set_dotenv_value(key: str, value: str, path: Path = ENV_PATH) -> None:
    """Set one key in .env, preserving unrelated lines."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output: list[str] = []
    replaced = False

    for line in lines:
        if line.strip().startswith(f"{key}="):
            output.append(f"{key}={value}")
            replaced = True
        else:
            output.append(line)

    if not replaced:
        if output and output[-1].strip():
            output.append("")
        output.append(f"{key}={value}")

    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.environ[key] = value


def token_preview(token: str) -> str:
    if not token:
        return "missing"
    if len(token) <= 12:
        return f"present len={len(token)}"
    return f"{token[:6]}...{token[-4:]} len={len(token)}"


def port_is_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def parse_proxy_port(proxy: str) -> int | None:
    if not proxy:
        return None
    tail = proxy.rsplit(":", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def vscode_remote_data_files() -> Iterable[Path]:
    base = Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage"
    remote_dir = base / "ms-vscode-remote.remote-ssh"
    if not remote_dir.exists():
        return []
    return sorted(remote_dir.rglob("data.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def terminal_ssh_socks_proxies() -> list[str]:
    """Find SOCKS ports from user-started `ssh -D ...` processes."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    proxies: list[str] = []
    for raw_command in result.stdout.splitlines():
        if "ssh" not in raw_command or "-D" not in raw_command:
            continue
        try:
            parts = shlex.split(raw_command)
        except ValueError:
            continue
        if not parts:
            continue

        for i, part in enumerate(parts):
            value = ""
            if part == "-D" and i + 1 < len(parts):
                value = parts[i + 1]
            elif part.startswith("-D") and len(part) > 2:
                value = part[2:]

            if not value:
                continue
            port_text = value.rsplit(":", 1)[-1]
            try:
                port = int(port_text)
            except ValueError:
                continue
            if port_is_open(DEFAULT_HOST, port):
                proxies.append(f"socks5h://127.0.0.1:{port}")

    # Preserve order while deduplicating.
    return list(dict.fromkeys(proxies))


def find_existing_socks_proxy(existing_proxy: str = "") -> str:
    """
    Find an already-running SOCKS proxy.

    This deliberately does not start SSH or perform login. It can reuse:
    - VS Code Remote-SSH's internal SOCKS proxy.
    - A user-started terminal tunnel such as `ssh -D 50700 107.ustc.edu.cn`.

    A plain `ssh 107.ustc.edu.cn` session has no SOCKS port and cannot be used
    as a local HTTP proxy.
    """
    existing_port = parse_proxy_port(existing_proxy)
    if existing_port and port_is_open(DEFAULT_HOST, existing_port):
        return existing_proxy

    terminal_proxies = terminal_ssh_socks_proxies()
    if terminal_proxies:
        return terminal_proxies[0]

    for data_file in vscode_remote_data_files():
        try:
            data = json.loads(data_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        port = data.get("socksPort")
        if isinstance(port, int) and port_is_open(DEFAULT_HOST, port):
            return f"socks5h://127.0.0.1:{port}"

    return existing_proxy


def find_vscode_socks_proxy(existing_proxy: str = "") -> str:
    """Backward-compatible alias for existing callers."""
    return find_existing_socks_proxy(existing_proxy)

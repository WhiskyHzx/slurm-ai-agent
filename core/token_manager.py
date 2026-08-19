#!/usr/bin/env python3
"""
Local SLURM_JWT management helpers.

The local app does not manage SSH login credentials. It can, however, reuse the
system `ssh` command to run `scontrol token ...` on the remote login node when
the user's SSH setup already supports non-interactive command execution.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from core.local_env import load_dotenv, set_dotenv_value, token_preview


DEFAULT_SSH_HOST = "107.ustc.edu.cn"
DEFAULT_TOKEN_LIFESPAN = 86400
DEFAULT_SSH_TIMEOUT = 20


def get_ssh_host() -> str:
    load_dotenv()
    return os.environ.get("SLURM_SSH_HOST", DEFAULT_SSH_HOST)


def get_token_lifespan() -> int:
    load_dotenv()
    raw = os.environ.get("SLURM_TOKEN_LIFESPAN", str(DEFAULT_TOKEN_LIFESPAN))
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_TOKEN_LIFESPAN


def remote_token_command(lifespan: int | None = None) -> str:
    lifespan = lifespan or get_token_lifespan()
    # 防御非法输入：确保 lifespan 是正整数，避免生成非法 scontrol 命令
    if lifespan <= 0:
        lifespan = DEFAULT_TOKEN_LIFESPAN
    return f"scontrol token lifespan={lifespan}"


@dataclass
class TokenStatus:
    present: bool
    preview: str
    expires_at: int | None
    seconds_remaining: int | None
    expired: bool | None
    refresh_command: str
    ssh_host: str


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}

    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get_token() -> str:
    load_dotenv()
    return os.environ.get("SLURM_JWT", "")


def get_token_status() -> TokenStatus:
    token = get_token()
    if not token:
        return TokenStatus(
            present=False,
            preview="missing",
            expires_at=None,
            seconds_remaining=None,
            expired=None,
            refresh_command=remote_token_command(),
            ssh_host=get_ssh_host(),
        )

    payload = _decode_jwt_payload(token)
    exp = payload.get("exp")
    expires_at = int(exp) if isinstance(exp, (int, float)) else None
    seconds_remaining = None
    expired = None
    if expires_at is not None:
        seconds_remaining = expires_at - int(time.time())
        expired = seconds_remaining <= 0

    return TokenStatus(
        present=True,
        preview=token_preview(token),
        expires_at=expires_at,
        seconds_remaining=seconds_remaining,
        expired=expired,
        refresh_command=remote_token_command(),
        ssh_host=get_ssh_host(),
    )


def normalize_token(raw_token: str) -> str:
    token = raw_token.strip().strip('"').strip("'")
    if token.startswith("SLURM_JWT="):
        token = token.split("=", 1)[1].strip().strip('"').strip("'")
    return token


def update_token(raw_token: str) -> TokenStatus:
    token = normalize_token(raw_token)
    if not token:
        raise ValueError("token is empty")
    if token.count(".") < 2:
        raise ValueError("token does not look like a JWT")

    set_dotenv_value("SLURM_JWT", token)
    return get_token_status()


def refresh_token_via_ssh(
    ssh_host: str | None = None,
    lifespan: int | None = None,
    timeout: int = DEFAULT_SSH_TIMEOUT,
) -> TokenStatus:
    """
    Generate a fresh SLURM_JWT through system ssh and store it in .env.

    This does not prompt for credentials. If SSH requires a password/passphrase
    and the user's shell has no suitable agent or ControlMaster, it fails with a
    clear error instead of hanging behind the web UI.
    """
    host = ssh_host or get_ssh_host()
    lifespan = lifespan or get_token_lifespan()
    command = remote_token_command(lifespan)

    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={min(timeout, 10)}",
            host,
            command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    output = (result.stdout or "").strip()
    if result.returncode != 0:
        detail = (result.stderr or output or "ssh command failed").strip()
        raise RuntimeError(detail)
    if not output:
        raise RuntimeError("ssh command returned no token")

    return update_token(output)

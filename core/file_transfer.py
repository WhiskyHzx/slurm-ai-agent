#!/usr/bin/env python3
"""
Server-side browser upload helpers.

Users explicitly pick local files in the browser. FastAPI receives those upload
streams on the SSH server, packages them, verifies the stored archive hash, and
extracts them into the user's project directory.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


DEFAULT_REMOTE_PROJECTS_BASE = "~/projects"
DEFAULT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_CONDA_PYTHON_VERSION = "3.10"
DEFAULT_CONDA_CREATE_TIMEOUT = 900
DEFAULT_CONDA_CHANNELS = "conda-forge"
CHUNK_SIZE = 1024 * 1024
MAX_PROJECT_NAME_LENGTH = 80


class FileTransferError(RuntimeError):
    """Raised when packaging, storing, or verification fails."""


@dataclass
class UploadResult:
    upload_id: str
    project_name: str
    file_count: int
    archive_name: str
    archive_size: int
    local_sha256: str
    remote_project_dir: str
    conda_env_dir: str
    conda_created: bool


@dataclass
class ProjectWorkspace:
    project_name: str
    project_dir: Path
    conda_env_dir: Path
    conda_created: bool


def get_max_upload_bytes() -> int:
    raw = os.environ.get("SLURM_UPLOAD_MAX_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))
    try:
        return max(1024, int(raw))
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES


def get_remote_projects_base() -> str:
    return os.environ.get("SLURM_REMOTE_PROJECTS_BASE", DEFAULT_REMOTE_PROJECTS_BASE).rstrip("/")


def get_conda_python_version() -> str:
    return os.environ.get("SLURM_PROJECT_CONDA_PYTHON", DEFAULT_CONDA_PYTHON_VERSION)


def get_conda_create_timeout() -> int:
    raw = os.environ.get("SLURM_CONDA_CREATE_TIMEOUT", str(DEFAULT_CONDA_CREATE_TIMEOUT))
    try:
        return max(60, int(raw))
    except ValueError:
        return DEFAULT_CONDA_CREATE_TIMEOUT


def get_conda_channels() -> list[str]:
    raw = os.environ.get("SLURM_CONDA_CHANNELS", DEFAULT_CONDA_CHANNELS)
    return [channel.strip() for channel in raw.split(",") if channel.strip()]


def find_conda_executable() -> str:
    configured = os.environ.get("SLURM_CONDA_EXE", "").strip()
    candidates = [
        configured,
        shutil.which("conda") or "",
        str(Path.home() / "miniconda3" / "bin" / "conda"),
        str(Path.home() / "miniforge3" / "bin" / "conda"),
        str(Path.home() / "anaconda3" / "bin" / "conda"),
    ]
    for candidate in candidates:
        candidate_path = Path(candidate).expanduser() if candidate else None
        if candidate_path and candidate_path.exists():
            return str(candidate_path)
    raise FileTransferError(
        "未找到 conda。请先安装 Miniconda，或设置 SLURM_CONDA_EXE=/path/to/conda"
    )


def normalize_project_name(raw_name: str) -> str:
    name = raw_name.strip()
    if not name:
        raise FileTransferError("请先输入作业目录名称")

    name = re.sub(r"\s+", "-", name)
    name = "".join(ch for ch in name if ch.isalnum() or ch in "._-")
    name = name.strip("._-")

    if not name:
        raise FileTransferError("作业目录名称只能包含文字、数字、点、下划线或短横线")
    if len(name) > MAX_PROJECT_NAME_LENGTH:
        raise FileTransferError(f"作业目录名称过长，最多 {MAX_PROJECT_NAME_LENGTH} 个字符")
    return name


def safe_relative_path(raw_name: str) -> Path:
    """
    Convert a browser-provided upload name into a safe relative path.

    Browsers may pass either `file.name` or `webkitRelativePath`. Reject absolute
    paths and parent traversal so uploaded archives cannot write outside staging.
    """
    cleaned = raw_name.replace("\\", "/").strip()
    if not cleaned:
        raise FileTransferError("上传文件名为空")

    rel = PurePosixPath(cleaned)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise FileTransferError(f"不安全的上传路径：{raw_name}")
    if ".slurm-agent" in rel.parts:
        raise FileTransferError("上传内容不能包含 .slurm-agent 目录")

    return Path(*rel.parts)


def resolve_server_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_archive(source_dir: Path, archive_path: Path) -> None:
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(source_dir))


def project_workspace(project_name: str) -> tuple[str, Path, Path]:
    safe_project_name = normalize_project_name(project_name)
    projects_base = resolve_server_path(get_remote_projects_base())
    project_dir = (projects_base / safe_project_name).resolve()
    if not str(project_dir).startswith(str(projects_base) + os.sep):
        raise FileTransferError("项目目录解析失败")
    return safe_project_name, project_dir, project_dir / ".slurm-agent" / "conda-env"


def ensure_project_workspace(project_name: str) -> ProjectWorkspace:
    safe_project_name, project_dir, conda_env_dir = project_workspace(project_name)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".slurm-agent").mkdir(parents=True, exist_ok=True)
    conda_created = ensure_conda_room(conda_env_dir)
    return ProjectWorkspace(
        project_name=safe_project_name,
        project_dir=project_dir,
        conda_env_dir=conda_env_dir,
        conda_created=conda_created,
    )


def ensure_conda_room(conda_env_dir: Path) -> bool:
    """Create the per-project conda environment if it does not already exist."""
    if (conda_env_dir / "conda-meta" / "history").exists():
        return False

    conda_env_dir.parent.mkdir(parents=True, exist_ok=True)
    conda_exe = find_conda_executable()
    python_version = get_conda_python_version()
    channels = get_conda_channels()
    channel_args = ["--override-channels"]
    for channel in channels:
        channel_args.extend(["-c", channel])
    result = subprocess.run(
        [
            conda_exe,
            "create",
            "-y",
            *channel_args,
            "-p",
            str(conda_env_dir),
            f"python={python_version}",
        ],
        capture_output=True,
        text=True,
        timeout=get_conda_create_timeout(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "conda create failed").strip()
        if "Terms of Service have not been accepted" in detail or "CondaToSNonInteractiveError" in detail:
            raise FileTransferError(
                "创建项目 Conda 环境失败：当前 Conda 默认源需要先接受 Anaconda 服务条款。"
                "本项目已默认使用 conda-forge 并覆盖默认源；如果你仍看到这个错误，"
                "请检查 SLURM_CONDA_CHANNELS 是否包含 repo.anaconda.com，或在终端执行："
                "\nconda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main"
                "\nconda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r"
            )
        raise FileTransferError(f"创建项目 Conda 环境失败: {detail[-1000:]}")
    return True


def extract_archive_to_project(
    archive_path: Path,
    upload_id: str,
    project_name: str,
) -> UploadResult:
    workspace = ensure_project_workspace(project_name)
    project_dir = workspace.project_dir
    archive_dir = project_dir / ".slurm-agent" / "uploads"
    stored_archive = archive_dir / f"{upload_id}.tar.gz"

    local_hash = sha256_file(archive_path)
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(archive_path, stored_archive)

    stored_hash = sha256_file(stored_archive)
    if stored_hash != local_hash:
        raise FileTransferError("服务端 SHA256 与上传包不一致，上传可能损坏")

    with tarfile.open(stored_archive, "r:gz") as archive:
        archive.extractall(project_dir)
    stored_archive.unlink(missing_ok=True)

    return UploadResult(
        upload_id=upload_id,
        project_name=workspace.project_name,
        file_count=0,
        archive_name=archive_path.name,
        archive_size=archive_path.stat().st_size,
        local_sha256=local_hash,
        remote_project_dir=str(project_dir),
        conda_env_dir=str(workspace.conda_env_dir),
        conda_created=workspace.conda_created,
    )


def package_and_upload(staging_dir: Path, file_count: int, project_name: str) -> UploadResult:
    if file_count <= 0:
        raise FileTransferError("没有可上传的文件")

    upload_id = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix=f"slurm-agent-{upload_id}-") as tmp:
        archive_path = Path(tmp) / f"{upload_id}.tar.gz"
        make_archive(staging_dir, archive_path)
        result = extract_archive_to_project(archive_path, upload_id, project_name)
        result.file_count = file_count
        return result

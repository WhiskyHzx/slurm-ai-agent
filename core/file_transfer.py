#!/usr/bin/env python3
"""
Server-side browser upload helpers.

Users explicitly pick local files in the browser. FastAPI receives those upload
streams on the SSH server, stages them into a temp directory, and copies them
straight into the user's project directory (merge-overwrite semantics).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platform fallback
    fcntl = None


DEFAULT_REMOTE_PROJECTS_BASE = "~/projects"
DEFAULT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_UPLOAD_FILES = 1000
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
    total_bytes: int
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


def get_max_upload_files() -> int:
    """单次上传允许的最大文件数量（默认 1000）。"""
    raw = os.environ.get("SLURM_UPLOAD_MAX_FILES", str(DEFAULT_MAX_UPLOAD_FILES))
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_UPLOAD_FILES


def human_size(num_bytes: int) -> str:
    """把字节数转成人类可读的 MB/GB 字符串。"""
    if num_bytes >= 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024 * 1024):.1f} GB"
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


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


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def project_lock(project_dir: Path, timeout: float = 10.0):
    """
    Serialize conda/pip mutations for one project (across threads and processes).

    FastAPI runs sync endpoints in a thread pool, so the same project can be
    mutated concurrently by two requests; separate processes (extra workers)
    are also possible. A thread lock plus an flock on
    <project>/.slurm-agent/install.lock covers both cases.
    """
    lock_path = project_dir / ".slurm-agent" / "install.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    lock_key = str(lock_path)
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(lock_key, threading.Lock())

    deadline = time.monotonic() + timeout
    while not thread_lock.acquire(timeout=0.5):
        if time.monotonic() >= deadline:
            raise FileTransferError("该项目正在执行另一个安装/初始化任务，请稍后重试")
    try:
        fd = None
        try:
            if fcntl is not None:
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise FileTransferError(
                                "该项目正在被其他进程安装/初始化，请稍后重试"
                            )
                        time.sleep(0.5)
            yield
        finally:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
    finally:
        thread_lock.release()


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
    _write_activate_script(project_dir, conda_env_dir)
    conda_created = ensure_conda_room(conda_env_dir)
    return ProjectWorkspace(
        project_name=safe_project_name,
        project_dir=project_dir,
        conda_env_dir=conda_env_dir,
        conda_created=conda_created,
    )


ACTIVATE_SCRIPT_FILENAME = "activate.sh"


def _write_activate_script(project_dir: Path, conda_env_dir: Path) -> None:
    """在项目根目录生成 activate.sh，方便用户在命令行一键激活项目环境。

    脚本不硬编码 conda 安装路径，而是用 `conda info --base` 动态定位 conda.sh，
    因此对 miniconda3 / miniforge3 / anaconda3 等安装位置都适用。
    """
    script = f"""#!/usr/bin/env bash
# 激活本项目专属的 Conda 环境（由 slurm-ai-agent 自动生成，可安全覆盖）。
# 用法：在项目目录里执行  source activate.sh
# 之后 `python` 即指向项目环境，`import numpy` 等依赖才能找到。

set -u

# 定位 conda：优先用 PATH 里的 conda，否则按常见安装位置探测
# （非交互式 SSH 会话里 conda 往往不在 PATH，需要显式探测）。
CONDA_BIN="$(command -v conda 2>/dev/null || true)"
if [ -z "$CONDA_BIN" ]; then
  for _c in "$HOME/miniconda3/bin/conda" "$HOME/miniforge3/bin/conda" "$HOME/anaconda3/bin/conda"; do
    if [ -x "$_c" ]; then
      CONDA_BIN="$_c"
      break
    fi
  done
fi

if [ -z "$CONDA_BIN" ]; then
  echo "错误：找不到 conda，请先安装 Miniconda 或把 conda 加入 PATH。" >&2
  return 1 2>/dev/null || exit 1
fi

CONDA_BASE="$("$CONDA_BIN" info --base 2>/dev/null)"
if [ -z "$CONDA_BASE" ]; then
  echo "错误：无法确定 conda base 路径。" >&2
  return 1 2>/dev/null || exit 1
fi

source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "{conda_env_dir}"

echo "已激活项目环境：{conda_env_dir}"
echo "当前 python：$(command -v python)"
"""
    target = project_dir / ACTIVATE_SCRIPT_FILENAME
    target.write_text(script, encoding="utf-8")
    try:
        target.chmod(0o644)
    except OSError:
        pass


def ensure_project_directory(project_name: str) -> ProjectWorkspace:
    """Create only the project directory and metadata directory; do not create conda."""
    safe_project_name, project_dir, conda_env_dir = project_workspace(project_name)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".slurm-agent").mkdir(parents=True, exist_ok=True)
    _write_activate_script(project_dir, conda_env_dir)
    return ProjectWorkspace(
        project_name=safe_project_name,
        project_dir=project_dir,
        conda_env_dir=conda_env_dir,
        conda_created=False,
    )


def ensure_conda_room(conda_env_dir: Path) -> bool:
    """Create the per-project conda environment if it does not already exist."""
    if (conda_env_dir / "conda-meta" / "history").exists():
        return False

    conda_env_dir.parent.mkdir(parents=True, exist_ok=True)
    # conda_env_dir = <project>/.slurm-agent/conda-env，项目锁放在 <project>/.slurm-agent/ 下
    with project_lock(conda_env_dir.parent.parent):
        # 拿到锁后双重检查：可能已有并发请求完成创建
        if (conda_env_dir / "conda-meta" / "history").exists():
            return False
        # 自愈：上次 conda create 中断会留下“有文件但无 conda-meta/history”的半成品环境，
        # 直接再 create 会报 prefix already exists，先清理后重建
        if conda_env_dir.exists():
            shutil.rmtree(conda_env_dir)
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


def copy_files_to_project(staging_dir: Path, file_count: int, project_name: str, subdir: str = "") -> UploadResult:
    """
    把 staging 暂存目录直接拷入项目目录（或项目内某个小文件夹）。

    增量覆盖语义（与原 tar 解压一致）：同名目录合并、同名文件覆盖写入、
    项目里旧有而本次未上传的文件保留，不会先清空项目目录。

    subdir 非空时自动创建目标小文件夹（拖拽上传新数据集场景）。
    """
    if file_count <= 0:
        raise FileTransferError("没有可上传的文件")

    workspace = ensure_project_directory(project_name)
    subdir = (subdir or "").strip().strip("/")
    if subdir:
        if subdir.startswith(".") or ".." in subdir.split("/") or "/" in subdir:
            raise FileTransferError(f"非法的小文件夹名称: {subdir}")
        if subdir in ("logs", "runs"):
            raise FileTransferError(f"不允许上传到运行产物目录: {subdir}")
    target_dir = workspace.project_dir / subdir if subdir else workspace.project_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = sum(p.stat().st_size for p in staging_dir.rglob("*") if p.is_file())
    # dirs_exist_ok=True → 合并而非报错；copy2 保留 mtime，便于用户排查文件新旧
    shutil.copytree(staging_dir, target_dir, dirs_exist_ok=True, copy_function=shutil.copy2)

    return UploadResult(
        upload_id=uuid.uuid4().hex[:12],
        project_name=workspace.project_name,
        file_count=file_count,
        total_bytes=total_bytes,
        remote_project_dir=str(target_dir),
        conda_env_dir=str(workspace.conda_env_dir),
        conda_created=workspace.conda_created,
    )

#!/usr/bin/env python3
"""
server/app.py — FastAPI 后端入口。

将 AgentLoop 包装成 HTTP API，提供：
  - POST /chat     发送消息，SSE 流式返回（含工具调用过程）
  - POST /reset    重置对话
  - GET  /         返回前端页面

启动方式：
  uvicorn server.app:app --host 0.0.0.0 --port 8080
"""

import asyncio
import json
import logging
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import getpass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.agent_loop import AgentLoop, SYSTEM_PROMPT
from agent.llm_provider import LLMProvider
from agent.tools_registry import TOOL_DEFINITIONS, ToolExecutor
from core.file_transfer import (
    CHUNK_SIZE,
    FileTransferError,
    ProjectWorkspace,
    copy_files_to_project,
    ensure_conda_room,
    ensure_project_directory,
    ensure_project_workspace,
    find_conda_executable,
    get_max_upload_bytes,
    get_remote_projects_base,
    project_lock,
    project_workspace,
    safe_relative_path,
)
from core.dependency_planner import (
    DependencyItem,
    _extract_json_object,
    installed_packages_snapshot,
    items_to_markdown,
    merge_dependency_items,
    parse_ai_dependency_items,
    precheck_dependencies,
    scan_project_dependencies,
    scan_user_dependency_notes,
    search_package_versions,
    serialize_items,
)
from config.model_config import (
    ensure_model_config_current,
    refresh_model_config,
    set_selected_model,
)
from core.slurm_client import SlurmClient, refresh_slurm_token, token_preview

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
app = FastAPI(title="算力平台智能助手", version="1.0")

# ---------------------------------------------------------------------------
# Agent 实例（未选择项目时使用默认会话；选择项目后每个作业目录一个会话）
# ---------------------------------------------------------------------------
agent: Optional[AgentLoop] = None
project_agents: dict[str, AgentLoop] = {}
conda_init_jobs: dict[str, dict] = {}
conda_init_jobs_lock = threading.Lock()


def _conda_env_ready(conda_env_dir: Path) -> bool:
    return (conda_env_dir / "conda-meta" / "history").exists()


def _conda_status_for(project_name: str) -> dict:
    safe_project_name, project_dir, conda_env_dir = project_workspace(project_name)
    key = safe_project_name
    if _conda_env_ready(conda_env_dir):
        return {
            "status": "ready",
            "project_name": safe_project_name,
            "project_dir": str(project_dir),
            "conda_env_dir": str(conda_env_dir),
            "message": "项目 Conda 环境已就绪",
        }
    with conda_init_jobs_lock:
        job = conda_init_jobs.get(key)
        if job and job.get("status") in {"initializing", "failed"}:
            return {
                "project_name": safe_project_name,
                "project_dir": str(project_dir),
                "conda_env_dir": str(conda_env_dir),
                **job,
            }
    return {
        "status": "missing",
        "project_name": safe_project_name,
        "project_dir": str(project_dir),
        "conda_env_dir": str(conda_env_dir),
        "message": "项目 Conda 环境尚未初始化",
    }


def _start_conda_init(project_name: str) -> dict:
    safe_project_name, project_dir, conda_env_dir = project_workspace(project_name)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".slurm-agent").mkdir(parents=True, exist_ok=True)
    if _conda_env_ready(conda_env_dir):
        return _conda_status_for(safe_project_name)

    with conda_init_jobs_lock:
        current = conda_init_jobs.get(safe_project_name)
        if current and current.get("status") == "initializing":
            return _conda_status_for(safe_project_name)
        conda_init_jobs[safe_project_name] = {
            "status": "initializing",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "message": "项目 Conda 环境正在后台初始化",
        }

    def worker() -> None:
        try:
            created = ensure_conda_room(conda_env_dir)
            with conda_init_jobs_lock:
                conda_init_jobs[safe_project_name] = {
                    "status": "ready",
                    "started_at": conda_init_jobs.get(safe_project_name, {}).get("started_at"),
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "created": created,
                    "message": "项目 Conda 环境已就绪",
                }
        except Exception as e:
            logger.exception("后台初始化项目 Conda 环境失败")
            with conda_init_jobs_lock:
                conda_init_jobs[safe_project_name] = {
                    "status": "failed",
                    "started_at": conda_init_jobs.get(safe_project_name, {}).get("started_at"),
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "error": str(e),
                    "message": f"项目 Conda 环境初始化失败: {e}",
                }

    threading.Thread(target=worker, daemon=True).start()
    return _conda_status_for(safe_project_name)


def _history_path(project_name: str, subdir: str = "", create: bool = False) -> Path:
    """会话记录路径：项目根会话存 .slurm-agent/chat-history.json，
    小文件夹（数据集组）会话存 .slurm-agent/sessions/<子目录>.json，不污染数据集目录。"""
    safe_project_name, project_dir, _ = project_workspace(project_name)
    if create:
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / ".slurm-agent").mkdir(parents=True, exist_ok=True)
    subdir = (subdir or "").strip().strip("/")
    if not subdir:
        return project_dir / ".slurm-agent" / "chat-history.json"
    if subdir.startswith(".") or ".." in subdir.split("/") or "/" in subdir:
        raise FileTransferError(f"非法的小文件夹名称: {subdir}")
    if create:
        (project_dir / ".slurm-agent" / "sessions").mkdir(parents=True, exist_ok=True)
    return project_dir / ".slurm-agent" / "sessions" / f"{subdir}.json"


def _read_chat_history(project_name: str, subdir: str = "") -> list[dict]:
    path = _history_path(project_name, subdir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    history = []
    for item in data[-200:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "ai"} and isinstance(content, str):
            history.append({"role": role, "content": content})
    return history


def _write_chat_history(project_name: str, history: list[dict], subdir: str = "") -> None:
    path = _history_path(project_name, subdir, create=True)
    path.write_text(
        json.dumps(history[-200:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _append_chat_history(project_name: str, role: str, content: str, subdir: str = "") -> None:
    if role not in {"user", "ai"} or not content.strip():
        return
    history = _read_chat_history(project_name, subdir)
    history.append({
        "role": role,
        "content": content,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    _write_chat_history(project_name, history, subdir)


def _agent_from_history(project_name: str, subdir: str = "") -> AgentLoop:
    safe_project_name, _, _ = project_workspace(project_name)
    safe_subdir = (subdir or "").strip().strip("/")
    executor = ToolExecutor(
        submit_handler=_submit_controlled_job,
        submission_context={
            "project_name": safe_project_name,
            "subdir": safe_subdir,
        },
    )
    ag = AgentLoop(executor=executor)
    for item in _read_chat_history(safe_project_name, safe_subdir)[-40:]:
        role = "assistant" if item["role"] == "ai" else "user"
        ag.messages.append({"role": role, "content": item["content"]})
    return ag


def get_agent(project_name: str = "", subdir: str = "") -> AgentLoop:
    """获取或懒初始化 AgentLoop；每个项目/小文件夹绑定独立受控提交上下文。"""
    global agent
    if project_name:
        safe_project_name, project_dir, _ = project_workspace(project_name)
        safe_subdir = (subdir or "").strip().strip("/")
        _resolve_run_dir(project_dir, safe_subdir)
        cache_key = f"{safe_project_name}/{safe_subdir}" if safe_subdir else safe_project_name
        if cache_key not in project_agents:
            project_agents[cache_key] = _agent_from_history(safe_project_name, safe_subdir)
        return project_agents[cache_key]
    if agent is None:
        agent = AgentLoop()
    return agent


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    project_name: str = ""
    subdir: str = ""


class ResetResponse(BaseModel):
    status: str


class ProjectCreateRequest(BaseModel):
    name: str
    environment_requirements: str = ""
    compute_requirements: str = ""


class ProjectReportRequest(BaseModel):
    name: str
    extra_notes: str = ""


class ProjectInstallRequest(BaseModel):
    name: str
    plan: str
    selected_items: list[dict] = []


class JobBodyRequest(BaseModel):
    name: str
    form: dict = {}
    subdir: str = ""


class ProjectChatAppendRequest(BaseModel):
    project_name: str
    role: str
    content: str
    subdir: str = ""


class JobSubmitRequest(BaseModel):
    project_name: str
    command: str
    job_name: str
    partition: str
    account: str
    qos: str
    nodes: int = 1
    cpus_per_task: int = 1
    gpus_per_node: int = 0
    memory_mb: int = 16384
    time_limit: int = 240  # 分钟
    subdir: str = ""


class ModelSelectRequest(BaseModel):
    model: str


class SubdirCreateRequest(BaseModel):
    project_name: str
    name: str = ""  # 空 = 自动取名 数据集N


class SubdirRenameRequest(BaseModel):
    project_name: str
    subdir: str
    new_name: str


class SubdirDeleteRequest(BaseModel):
    project_name: str
    subdir: str


class JobTemplateSaveRequest(BaseModel):
    name: str
    # 兼容旧客户端直接提交完整脚本；新客户端只提交结构化草稿，
    # 完整模板脚本由服务端按受控提交规则生成。
    content: str = ""
    project_name: str = ""
    subdir: str = ""
    command: str = ""
    job_name: str = ""
    partition: str = ""
    account: str = ""
    qos: str = ""
    nodes: int = 1
    cpus_per_task: int = 1
    gpus_per_node: int = 0
    memory_mb: int = 16384
    time_limit: int = 240


class JobTemplateDeleteRequest(BaseModel):
    name: str


PROJECT_NOTES_FILENAME = "PROJECT_NOTES.txt"

# 项目内不作为数据集小文件夹展示的目录（服务自身/运行产物）
# 子目录黑名单：.slurm-agent 服务内部目录；logs 运行产物；runs 历史遗留目录名（继续隐藏旧项目的残留目录）
SUBDIR_EXCLUDE_NAMES = {".slurm-agent", "logs", "runs"}


def _project_subdirs(project_dir: Path, limit: int = 20) -> list[str]:
    """扫描项目内的一级子目录作为数据集小文件夹（按修改时间倒序，新建的在前），
    隐藏目录与运行产物目录除外。"""
    candidates: list[tuple[float, str]] = []
    try:
        for path in project_dir.iterdir():
            if not path.is_dir() or path.name.startswith(".") or path.name in SUBDIR_EXCLUDE_NAMES:
                continue
            try:
                candidates.append((path.stat().st_mtime, path.name))
            except OSError:
                continue
    except OSError:
        return []
    candidates.sort(reverse=True)
    return [name for _, name in candidates[:limit]]


def _resolve_run_dir(project_dir: Path, subdir: str = "") -> Path:
    """把可选的小文件夹名解析为安全的作业运行目录。"""
    subdir = (subdir or "").strip().strip("/")
    if not subdir:
        return project_dir
    if subdir.startswith(".") or ".." in subdir.split("/") or "/" in subdir:
        raise FileTransferError(f"非法的小文件夹名称: {subdir}")
    run_dir = project_dir / subdir
    if not run_dir.is_dir():
        raise FileTransferError(f"小文件夹不存在: {subdir}（可先上传文件夹后重试）")
    return run_dir


def _validate_subdir_name(name: str, label: str = "小文件夹") -> str:
    """校验小文件夹/模板等单级名称：非空、无路径分隔、非隐藏/保留名，返回清洗后的名字。"""
    name = (name or "").strip().strip("/")
    if not name:
        raise FileTransferError(f"{label}名称不能为空")
    if len(name) > 64:
        raise FileTransferError(f"{label}名称过长（最多 64 字符）")
    if name.startswith(".") or "/" in name or "\\" in name or ".." in name:
        raise FileTransferError(f"非法的{label}名称: {name}")
    if name in SUBDIR_EXCLUDE_NAMES:
        raise FileTransferError(f"名称为保留目录: {name}")
    return name


def _subdir_session_path(project_dir: Path, subdir: str) -> Path:
    return project_dir / ".slurm-agent" / "sessions" / f"{subdir}.json"

# QoS 资源上限静态兑底表：来源 docs/docs-main/docs/overview/resources.md「平台内置 QOS 方案示例」，
# 仅在 REST /qos 不可用时使用，正常运行时以 Slurm 实时数据为准。
STATIC_QOS_LIMITS = {
    "qos_stu_default": {"cpu": 4, "gpu": 1, "mem_mb": 16384, "wall_minutes": 240},
    "qos_stu_small": {"cpu": 8, "gpu": 1, "mem_mb": 32768, "wall_minutes": 480},
    "qos_stu_medium_2gpu": {"cpu": 24, "gpu": 2, "mem_mb": 131072, "wall_minutes": 720},
    "qos_stu_long": {"cpu": 16, "gpu": 1, "mem_mb": 65536, "wall_minutes": 4320},
    "qos_stu_cpu_long": {"cpu": 32, "gpu": 0, "mem_mb": 131072, "wall_minutes": 4320},
}
MAX_CONTEXT_TEXT_CHARS = 18000
MAX_BASH_FILES = 12
MAX_BASH_FILE_CHARS = 3000
MAX_PACKAGE_QUERIES = 6
MAX_READABLE_TEXT_FILES = 24
MAX_READABLE_TEXT_FILE_CHARS = 5000

# ---------------------------------------------------------------------------
# 集群硬件上下文：分区 GPU 型号 → conda 包需要的 CUDA 构建下限。
# 目的：防止 LLM 凭旧知识选包（如给 RTX 5090 选 CUDA 11 构建）。
# RTX 5090 是 Blackwell（sm_120），需要 CUDA >= 12.8 编译的包才能用 GPU；
# A100 是 Ampere（sm_80），CUDA >= 11 的构建即可。
# ---------------------------------------------------------------------------
PARTITION_GPU_CUDA_REQUIREMENTS = {
    "GPU-RTX5090": "RTX 5090（Blackwell，sm_120）— 必须选 CUDA >= 12.8 编译的包构建",
    "P107-RTX5090": "RTX 5090（Blackwell，sm_120）— 必须选 CUDA >= 12.8 编译的包构建",
    "GPU-A100": "A100（Ampere，sm_80）— 需要 CUDA >= 11.0 编译的包构建",
    "P107-A100": "A100（Ampere，sm_80）— 需要 CUDA >= 11.0 编译的包构建",
}


def _hardware_context_text() -> str:
    lines = [
        "集群分区与 GPU 硬件（选包/选构建时必须遵守）：",
        "- CPU-6530 / CPU-8358P / Students：无 GPU，选 CPU 构建（nompi 或 openmpi，不带 cuda）",
    ]
    for partition, requirement in PARTITION_GPU_CUDA_REQUIREMENTS.items():
        lines.append(f"- {partition}：{requirement}")
    lines.append(
        "选包规则：涉及 GPU 计算的包（如 pytorch、gromacs、tensorflow），必须根据目标分区的 GPU "
        "在包管理查询结果里选择满足 CUDA 要求的构建变体（conda 的 build 字段，如 nompi_cuda、cuda126）；"
        "查询结果中查不到满足要求的构建时，版本留空并写入需确认问题，不要猜。"
    )
    return "\n".join(lines)
READABLE_TEXT_SUFFIXES = {
    ".sh", ".bash", ".sbatch", ".txt", ".md", ".rst", ".log",
    ".py", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".java",
    ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".cmake", ".mk", ".makefile",
    ".r", ".m", ".jl", ".f", ".f90", ".for",
}
DATA_OR_SPECIAL_SUFFIXES = {
    ".csv", ".tsv", ".dat", ".bin", ".npy", ".npz", ".h5", ".hdf5",
    ".nc", ".mat", ".pdb", ".xtc", ".trr", ".tpr", ".edr", ".gro",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".doc", ".docx",
    ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".tar", ".gz", ".bz2",
    ".xz", ".7z", ".rar", ".so", ".dylib", ".o", ".a", ".exe",
}


def _state_text(value) -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, list):
        return ",".join(str(v) for v in value) or "UNKNOWN"
    if isinstance(value, dict):
        return str(value.get("current") or value.get("state") or value)
    return str(value)


def _number(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _gpu_count_from_text(value) -> int:
    text = str(value or "")
    match = re.search(r"(?:gres/)?gpu(?:[:=][^,:()]+)?[:=](\d+)", text, re.IGNORECASE)
    if match:
        return _number(match.group(1))
    return 0


def _summarize_node(node: dict) -> dict:
    state = _state_text(node.get("state") or node.get("state_flags"))
    cpus = _number(node.get("cpus") or node.get("cpu_count"))
    alloc_cpus = _number(node.get("alloc_cpus") or node.get("allocated_cpus"))
    memory = _number(node.get("real_memory") or node.get("memory"))
    alloc_memory = _number(node.get("alloc_memory") or node.get("allocated_memory"))
    gres = node.get("gres") or node.get("active_features") or ""
    tres = node.get("tres") or ""
    gres_used = node.get("gres_used") or ""
    tres_used = node.get("tres_used") or ""
    gpus = _gpu_count_from_text(gres) or _gpu_count_from_text(tres)
    alloc_gpus = _gpu_count_from_text(gres_used) or _gpu_count_from_text(tres_used)
    return {
        "name": node.get("name") or node.get("hostname") or "-",
        "state": state,
        "partition": node.get("partition") or node.get("partitions") or "-",
        "cpus": cpus,
        "alloc_cpus": alloc_cpus,
        "memory": memory,
        "alloc_memory": alloc_memory,
        "gres": gres,
        "gpus": gpus,
        "alloc_gpus": alloc_gpus,
    }


def _ts_seconds(value) -> int:
    """从 REST 时间戳结构（{set, infinite, number}）或裸数字中提取 unix 秒。"""
    if isinstance(value, dict):
        return _number(value.get("number"))
    return _number(value)


def _summarize_job(job: dict) -> dict:
    state = _state_text(job.get("job_state") or job.get("state"))
    now = datetime.now().timestamp()
    start = _ts_seconds(job.get("start_time"))
    submit = _ts_seconds(job.get("submit_time"))
    # run_time 字段在当前 slurmrestd 版本恒为 null，用时间戳推算：
    # RUNNING 用 now - start_time，PENDING（start_time 为 0）用 now - submit_time 即排队时长
    run_seconds = max(0, int(now - start)) if state.startswith("RUNNING") and start else 0
    queue_seconds = max(0, int(now - submit)) if state.startswith("PENDING") and submit else 0
    return {
        "id": job.get("job_id") or job.get("jobid") or job.get("id") or "-",
        "name": job.get("name") or job.get("job_name") or "-",
        "user": job.get("user_name") or job.get("user") or "-",
        "partition": job.get("partition") or "-",
        "state": state,
        "nodes": job.get("nodes") or job.get("node_count") or "-",
        "time_limit": job.get("time_limit") or job.get("time_limit_number") or "-",
        "run_seconds": run_seconds,
        "queue_seconds": queue_seconds,
    }


def _count_by_state(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        primary = str(item.get("state") or "UNKNOWN").split(",", 1)[0].upper()
        counts[primary] = counts.get(primary, 0) + 1
    return counts


def _trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[内容过长，已截断]"


def _append_project_notes(
    project_dir: Path,
    environment_requirements: str = "",
    compute_requirements: str = "",
    extra_notes: str = "",
) -> Path:
    notes_path = project_dir / PROJECT_NOTES_FILENAME
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sections = []
    if environment_requirements.strip():
        sections.append(("环境依赖要求", environment_requirements.strip()))
    if compute_requirements.strip():
        sections.append(("算力特别需求", compute_requirements.strip()))
    if extra_notes.strip():
        sections.append(("补充说明", extra_notes.strip()))

    if not notes_path.exists():
        notes_path.write_text(
            "# 项目需求记录\n\n"
            "这里记录用户补充的环境依赖、算力需求和后续修改意见，供智能体生成依赖安装方案和作业布置建议使用。\n\n",
            encoding="utf-8",
        )

    if sections:
        with notes_path.open("a", encoding="utf-8") as f:
            f.write(f"## {timestamp}\n\n")
            for title, body in sections:
                f.write(f"### {title}\n{body}\n\n")
    return notes_path


def _is_probably_text(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    if not chunk:
        return True
    control_bytes = sum(1 for byte in chunk if byte < 9 or (13 < byte < 32))
    return control_bytes / max(1, len(chunk)) < 0.08


def _is_readable_text_file(path: Path) -> bool:
    if path.name.startswith("."):
        return False
    suffix = path.suffix.lower()
    if suffix in DATA_OR_SPECIAL_SUFFIXES:
        return False
    if path.name.lower() in {"makefile", "dockerfile", "cmakelists.txt"}:
        return _is_probably_text(path)
    if suffix in READABLE_TEXT_SUFFIXES:
        return _is_probably_text(path)
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:160]
    except OSError:
        return False
    if head.startswith("#!") and any(shell in head.splitlines()[0] for shell in ("sh", "bash", "python")):
        return True
    return False


def _project_tree(project_dir: Path) -> str:
    lines: list[str] = []
    skip_parts = {".git", "__pycache__", "conda-env", "uploads"}
    for path in sorted(project_dir.rglob("*")):
        rel = path.relative_to(project_dir)
        if ".slurm-agent" in rel.parts and len(rel.parts) > 1:
            continue
        if any(part in skip_parts for part in rel.parts):
            continue
        if len(lines) >= 160:
            lines.append("...[文件较多，已截断]")
            break
        suffix = "/" if path.is_dir() else ""
        lines.append(f"- {rel}{suffix}")
    return "\n".join(lines) or "- 目录暂时为空"


def _collect_readable_text_files(project_dir: Path) -> str:
    chunks: list[str] = []
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(project_dir)
        if ".slurm-agent" in rel.parts or ".git" in rel.parts:
            continue
        if not _is_readable_text_file(path):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        language = path.suffix.lower().lstrip(".") or "text"
        if path.name.lower() == "makefile":
            language = "makefile"
        chunks.append(
            f"### {rel}\n```{language}\n{_trim_text(content, MAX_READABLE_TEXT_FILE_CHARS)}\n```"
        )
        if len(chunks) >= MAX_READABLE_TEXT_FILES:
            chunks.append("...[可阅读文本文件较多，已截断]")
            break
    return "\n\n".join(chunks) or "未发现可直接阅读的文本/源码/脚本文件。"


def _infer_package_candidates(text: str) -> list[str]:
    aliases = {
        "pytorch": "pytorch",
        "torch": "pytorch",
        "tensorflow": "tensorflow",
        "gromacs": "gromacs",
        "cuda": "cuda-toolkit",
        "cudatoolkit": "cudatoolkit",
        "openmpi": "openmpi",
        "mpi": "openmpi",
        "numpy": "numpy",
        "scipy": "scipy",
        "pandas": "pandas",
    }
    candidates: list[str] = []
    lowered = text.lower()
    for token, package in aliases.items():
        if token in lowered and package not in candidates:
            candidates.append(package)

    for match in re.finditer(r"(?:conda|pip)\s+install\s+([a-z0-9_.-]+)", lowered):
        package = match.group(1).strip("._-")
        if package and package not in candidates:
            candidates.append(package)
    return candidates[:MAX_PACKAGE_QUERIES]


def _conda_package_queries(notes_text: str) -> str:
    candidates = _infer_package_candidates(notes_text)
    if not candidates:
        return "未从需求记录中识别到明确包名；报告中请给出需要用户确认的包管理查询命令。"

    try:
        conda_exe = find_conda_executable()
    except FileTransferError as e:
        return f"未执行 conda search：{e}"

    lines: list[str] = []
    for package in candidates:
        try:
            result = subprocess.run(
                [
                    conda_exe,
                    "search",
                    "--override-channels",
                    "-c",
                    "conda-forge",
                    package,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            lines.append(f"### {package}\nconda search 超时，请稍后手动确认。")
            continue
        except OSError as e:
            lines.append(f"### {package}\nconda search 执行失败：{e}")
            continue

        output = (result.stdout or result.stderr or "").strip()
        if result.returncode != 0:
            lines.append(f"### {package}\n查询失败：{_trim_text(output, 800)}")
        else:
            useful = "\n".join(output.splitlines()[-12:])
            lines.append(f"### {package}\n```text\n{_trim_text(useful, 1200)}\n```")
    return "\n\n".join(lines)


def _build_project_report_prompt(workspace, extra_notes: str = "") -> str:
    notes_path = workspace.project_dir / PROJECT_NOTES_FILENAME
    notes_text = notes_path.read_text(encoding="utf-8", errors="ignore") if notes_path.exists() else "暂无项目需求记录。"
    if extra_notes.strip():
        notes_text += f"\n\n## 本次追加意见\n{extra_notes.strip()}\n"

    context = f"""
你是运行在 USTC 107 算力平台上的 Slurm 项目准备助手。下面的输入结构是严格的，不要忽略任何字段。

<任务>
根据项目目录、用户输入记录、用户上传的依赖清单/算力需求文件、可直接阅读的文本/源码/脚本文件，以及包管理查询结果，生成依赖安装方案。
</任务>

<硬性规则>
1. 不要声称已经安装依赖、写入脚本或提交作业；这里只生成“将要安装什么”的方案和命令草案。
2. Slurm 标准输出和标准错误由系统统一写入 logs/<作业名>-%j.out 与 logs/<作业名>-%j.err，无需在方案中处理。
3. 程序结果直接写在当前运行目录（或程序自身输出参数指定的位置），不要创建额外的结果目录。
4. 每个项目的 conda 环境已准备在 <conda_env>，依赖安装命令优先使用该环境。
5. 如果依赖名称、入口命令、数据路径或算力需求不确定，必须列入“需要用户确认的问题”，不能擅自假设。
6. 如果包管理查询结果不足，请给出可复制的 conda/pip 查询命令。
7. “将要安装的程序环境列表”必须带版本或版本范围；版本号只能来自项目文件、用户输入或包管理查询结果中的真实版本，其它情况写“需确认”，不要编造。特别禁止把其它集群 module 系统里的版本号（如 gromacs/2019.4-gcc-9.2.0-openmpi 中的 2019.4）直接当作 conda/pip 可安装版本。
8. “安装命令”只能包含 conda/mamba/pip 安装命令，每行一条，不要写 rm、curl、wget、bash、sh、source、export 或其它 shell 操作。
9. 输出使用 Markdown，必须严格包含以下标题：
   - ## 1. 项目理解
   - ## 2. 将要安装的程序环境列表
   - ## 3. 安装命令
   - ## 4. 建议的算力配置
   - ## 5. 输出路径规范
   - ## 6. 需要用户确认的问题
   - ## 7. 后续布置作业说明
</硬性规则>

<项目元信息>
<folder_name>{workspace.project_name}</folder_name>
<folder_path>{workspace.project_dir}</folder_path>
<conda_env>{workspace.conda_env_dir}</conda_env>
</项目元信息>

<集群硬件上下文>
{_hardware_context_text()}
</集群硬件上下文>

<用户输入记录>
以下内容来自用户在创建作业目录、补充说明和后续修改意见中的所有文字输入：
{notes_text}
</用户输入记录>

<包管理查询结果>
{_conda_package_queries(notes_text)}
</包管理查询结果>

<项目目录摘要>
{_project_tree(workspace.project_dir)}
</项目目录摘要>

<可直接阅读的文本文件内容>
说明：这里收集非二进制、非明显数据文件、非特殊格式文件的内容，例如 .sh、.txt、.c、.cpp、.py、.md、配置文件等；明显数据文件和二进制文件已排除。
{_collect_readable_text_files(workspace.project_dir)}
</可直接阅读的文本文件内容>
"""
    return _trim_text(context, MAX_CONTEXT_TEXT_CHARS)


def _search_results_text(items: list[DependencyItem], notes_text: str) -> str:
    """
    汇总“包管理查询结果”：优先用预检拿到的真实版本/构建列表，
    再补充从需求记录识别出的包名查询（_conda_package_queries）。
    这是 LLM 选版本的事实依据，避免凭旧知识指定不存在的版本。
    """
    lines: list[str] = []
    for item in items:
        if item.precheck_status == "installed":
            lines.append(f"### {item.name}\n已安装在项目环境（{item.precheck_detail}），无需再选。")
            continue
        if item.available_versions:
            detail = f"可用版本（旧→新）：{item.available_versions}"
            if item.precheck_status == "version_mismatch":
                detail += f"；注意：请求版本 {item.version} 不存在，建议 {item.suggested_version}"
            lines.append(f"### {item.name}\n{detail}")
        elif item.precheck_status == "missing":
            lines.append(f"### {item.name}\n软件源中未找到该包，请确认包名或改用其它渠道。")
    # 需求记录里识别出、但不在扫描清单里的包也查一遍（如用户写了 gromacs）
    known = {item.name.lower() for item in items}
    extra_queries: list[str] = []
    for candidate in _infer_package_candidates(notes_text):
        if candidate.lower() not in known:
            extra_queries.append(candidate)
    for name in extra_queries[:MAX_PACKAGE_QUERIES]:
        search = search_package_versions(name)
        if search["ok"]:
            builds = ", ".join(search["builds"][-12:])
            lines.append(
                f"### {name}\n可用版本（旧→新）：{', '.join(search['versions'][-10:])}\n最近构建：{builds}"
            )
        else:
            lines.append(f"### {name}\n查询失败：{search['error']}")
    if not lines:
        return "未查询到可用版本信息；报告中请给出需要用户确认的包管理查询命令。"
    return "\n\n".join(lines)


def _build_ai_dependency_json_prompt(
    workspace,
    scanned_items: list[DependencyItem],
    extra_notes: str = "",
    notes_text: str = "",
) -> str:
    if not notes_text:
        notes_path = workspace.project_dir / PROJECT_NOTES_FILENAME
        notes_text = notes_path.read_text(encoding="utf-8", errors="ignore") if notes_path.exists() else "暂无项目需求记录。"
    if extra_notes.strip():
        notes_text += f"\n\n## 本次追加意见\n{extra_notes.strip()}\n"
    scanned_json = json.dumps(serialize_items(scanned_items), ensure_ascii=False, indent=2)
    context = f"""
你是 USTC 107 算力平台的依赖规划助手。请只补充“扫描结果中可能缺失”的依赖项，返回严格 JSON，不要 Markdown，不要解释。

返回格式：
[
  {{"name": "包名", "version": "版本或版本范围；未知留空", "manager": "conda 或 pip", "reason": "为什么需要"}}
]

规则：
1. 已在扫描结果中出现的依赖不要重复返回。
2. 只有当项目文本、用户需求或源码明显需要某个依赖时才返回。
3. **版本号只能来自 <包管理查询结果> 中的真实版本、项目文件或用户输入**；不确定就留空，禁止凭记忆编造。特别禁止把其它集群 module 系统的版本号（如 gromacs/2019.4-gcc-9.2.0-openmpi）当作可安装版本。
4. 涉及 GPU 的包：根据 <集群硬件上下文> 在查询结果的构建变体（build，如 nompi_cuda、cuda126、mpi_openmpi）里选择满足目标 GPU CUDA 要求的，并把构建写进 version（conda 三段式语法，如 "2026.3=nompi_cuda"）；查不到满足要求的构建就留空并在 reason 里说明。
5. 不要返回 python、pip、setuptools、wheel。
6. CUDA/PyTorch/TensorFlow 相关项要保守，版本不确定时写空。
7. 最多返回 20 项。

<已扫描依赖（含预检的真实版本信息）>
{scanned_json}
</已扫描依赖>

<包管理查询结果>
{_search_results_text(scanned_items, notes_text)}
</包管理查询结果>

<集群硬件上下文>
{_hardware_context_text()}
</集群硬件上下文>

<用户输入记录>
{notes_text}
</用户输入记录>

<当前运行目录树（根目录就是命令执行位置）>
{_project_tree(run_dir)}
</当前运行目录树>

<当前运行目录内可直接阅读的文本文件内容>
{_collect_readable_text_files(run_dir)}
</当前运行目录内可直接阅读的文本文件内容>
"""
    return _trim_text(context, MAX_CONTEXT_TEXT_CHARS)


def _extract_install_commands(plan: str) -> list[str]:
    commands: list[str] = []
    in_code = False
    for raw_line in str(plan or "").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        line = stripped.lstrip("$ ").strip()
        if not line or line.startswith("#"):
            continue
        lowered = line.lower()
        if not (
            lowered.startswith("conda install ")
            or lowered.startswith("mamba install ")
            or lowered.startswith("pip install ")
            or lowered.startswith("python -m pip install ")
        ):
            continue
        if any(token in lowered for token in ("&&", "||", ";", "|", "`", "$(", " rm ", " curl ", " wget ", " bash ", " sh ")):
            continue
        commands.append(line)
        if len(commands) >= 20:
            break
    return commands


def _normalize_install_command(command: str, conda_env_dir: Path) -> list[str]:
    parts = shlex.split(command)
    if not parts:
        raise FileTransferError("安装命令为空")

    conda_exe = find_conda_executable()
    head = parts[0].lower()
    lowered = [part.lower() for part in parts]
    if head in {"conda", "mamba"} and len(parts) >= 2 and parts[1].lower() == "install":
        package_args: list[str] = []
        channel_args: list[str] = []
        skip_next = False
        expect_channel = False
        for part in parts[2:]:
            lowered_part = part.lower()
            if skip_next:
                skip_next = False
                continue
            if expect_channel:
                if re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part.lower() not in {"defaults", "main", "r"}:
                    channel_args.extend(["-c", part])
                expect_channel = False
                continue
            if lowered_part in {"-y", "--yes", "--override-channels"}:
                continue
            if lowered_part in {"-p", "--prefix", "-n", "--name"}:
                skip_next = True
                continue
            if lowered_part in {"-c", "--channel"}:
                expect_channel = True
                continue
            package_args.append(part)
        if not channel_args:
            channel_args = ["-c", "conda-forge"]
        return [
            conda_exe,
            "install",
            "-y",
            "--override-channels",
            *channel_args,
            "-p",
            str(conda_env_dir),
            *package_args,
        ]

    if head == "pip" and len(parts) >= 2 and parts[1].lower() == "install":
        return [conda_exe, "run", "-p", str(conda_env_dir), "python", "-m", "pip", "install", *parts[2:]]

    if lowered[:4] == ["python", "-m", "pip", "install"]:
        return [conda_exe, "run", "-p", str(conda_env_dir), "python", "-m", "pip", "install", *parts[4:]]

    raise FileTransferError(f"不支持的安装命令：{command}")


def _commands_from_selected_items(selected_items: list[dict]) -> list[str]:
    """
    将勾选项合并为至多两条命令：conda 一条、pip 一条（conda 在前）。

    合并的理由：逐包分开安装会导致 conda 重复求解且后一次安装可能
    升降级前一次装的包；pip 安装后若再跑 conda install，conda 可能
    覆盖 pip 装的文件，因此固定 conda 先、pip 后。
    """
    conda_specs: list[str] = []
    pip_specs: list[str] = []
    for item in selected_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            continue
        version = str(item.get("version") or "").strip()
        manager = str(item.get("manager") or "conda").strip().lower()
        if manager not in {"conda", "pip"}:
            manager = "conda"
        spec = name
        if version and version.lower() not in {"需确认", "unknown", "none", "null"}:
            if manager == "pip":
                if re.fullmatch(r"[0-9][A-Za-z0-9.*_+!-]*", version):
                    # 裸版本号拼 ==，否则 name1.2.3 是非法 requirement
                    spec = f"{name}=={version}"
                else:
                    spec = f"{name}{version}"
            elif version.startswith("=="):
                spec = f"{name}={version[2:]}"
            elif version.startswith("="):
                # 以 = 开头的写法（如 =2026.3）直接拼接
                spec = f"{name}{version}"
            elif "=" in version:
                # 含 = 但不以 = 开头的是 version=build 三段式（如 2026.3=nompi_cuda），
                # 需要补一个 = 组成 name=version=build
                spec = f"{name}={version}"
            elif re.fullmatch(r"[0-9][A-Za-z0-9.*_+!-]*", version):
                spec = f"{name}={version}"
        (conda_specs if manager == "conda" else pip_specs).append(shlex.quote(spec))

    commands: list[str] = []
    if conda_specs[:40]:
        commands.append("conda install " + " ".join(conda_specs[:40]))
    if pip_specs[:40]:
        commands.append("pip install " + " ".join(pip_specs[:40]))
    return commands


def _validate_installed_items(selected_items: list[dict], conda_env_dir: Path) -> list[dict]:
    """用一次 conda list 快照校验所有勾选包，代替逐包查询。"""
    valid_names = []
    for item in selected_items[:40]:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                valid_names.append(name)
    if not valid_names:
        return []

    try:
        conda_exe = find_conda_executable()
    except FileTransferError as e:
        return [{"name": name, "status": "unknown", "detail": str(e)} for name in valid_names]

    installed: dict[str, str] = {}
    try:
        result = subprocess.run(
            [conda_exe, "list", "-p", str(conda_env_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        for line in (result.stdout or "").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                installed[parts[0].lower().replace("_", "-")] = parts[1]
    except subprocess.TimeoutExpired:
        return [{"name": name, "status": "unknown", "detail": "验证超时"} for name in valid_names]
    except OSError as e:
        return [{"name": name, "status": "unknown", "detail": str(e)} for name in valid_names]

    results: list[dict] = []
    for name in valid_names:
        key = name.lower().replace("_", "-")
        if key in installed:
            results.append({"name": name, "status": "ok", "detail": f"已安装 {installed[key]}"})
        else:
            results.append({"name": name, "status": "missing", "detail": "环境中未找到该包"})
    return results


def _light_workspace(project_name: str) -> ProjectWorkspace:
    """解析工作区并确保目录存在，但不触发 conda create（用于轻量只读接口）。"""
    safe_project_name, project_dir, conda_env_dir = project_workspace(project_name)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".slurm-agent").mkdir(parents=True, exist_ok=True)
    return ProjectWorkspace(
        project_name=safe_project_name,
        project_dir=project_dir,
        conda_env_dir=conda_env_dir,
        conda_created=False,
    )


def _conda_sh_path() -> str:
    """
    返回可 source 的 conda.sh 绝对路径。

    现代 conda 的环境目录里不再有 bin/activate，正确做法是 source
    base 安装下的 etc/profile.d/conda.sh 再 conda activate <prefix>。
    """
    conda_exe = Path(find_conda_executable()).resolve()
    candidate = conda_exe.parent.parent / "etc" / "profile.d" / "conda.sh"
    if candidate.exists():
        return str(candidate)
    # 兜底：作业里动态展开 base 路径（要求计算节点 conda 在 PATH）
    return "$(conda info --base)/etc/profile.d/conda.sh"


def _build_job_body_prompt(workspace: ProjectWorkspace, form: dict, run_dir: Path = None) -> str:
    notes_path = workspace.project_dir / PROJECT_NOTES_FILENAME
    notes_text = notes_path.read_text(encoding="utf-8", errors="ignore") if notes_path.exists() else ""
    installed = installed_packages_snapshot(workspace.conda_env_dir)
    installed_text = ", ".join(sorted(installed)[:150]) or "（环境为空或尚未安装依赖）"
    form_lines = [f"- {key}: {value}" for key, value in (form or {}).items() if value not in (None, "")]
    form_text = "\n".join(form_lines) or "（用户未调整，均为默认值）"
    python_bin = workspace.conda_env_dir / "bin" / "python"
    run_dir = run_dir or workspace.project_dir
    context = f"""
你是 USTC 107 算力平台的 Slurm 作业命令生成器。请根据项目内容，生成 sbatch 脚本的“作业命令正文”。

<硬性规则>
1. 只输出作业命令正文（bash 命令与注释），不要输出 #!/bin/bash 和任何 #SBATCH 行——头部由系统生成。
2. 不要输出 cd、mkdir、conda 激活、source 等环境准备命令——系统已在正文之前固定处理：工作目录已切到运行目录，项目 Conda 环境已激活。
3. 主计算命令用 srun 开头（如 srun python -u train.py --epochs 10）。
4. 下方目录树的根就是当前运行目录；所有相对路径必须直接以该目录为基准，绝不能再次添加运行目录自身的文件夹名。
5. 只能引用目录树中确实存在的入口脚本和配置文件，不要猜测 main.py、train.py 或配置路径。
6. python 命令加 -u 实时输出；正文开头结尾可用 echo 打印时间戳。
7. 默认只生成完成训练所必需的命令。除非用户明确要求，不要额外创建结果目录、复制 outputs、隐藏错误或添加 || true。
8. 除非用户明确要求，不要创建额外结果目录；程序输出直接写当前运行目录或使用程序自身支持的输出参数，不要臆造程序不支持的参数。
9. 入口脚本、参数不确定时选最合理的默认，并在注释中标注“默认值，可修改”。
10. 只输出代码本身，不要 Markdown 代码块标记，不要解释文字。
</硬性规则>

<项目元信息>
- 运行目录（作业 cd 后所在，数据集/入口脚本以此为准）：{run_dir}
- 项目 Conda 环境（整个项目共享，勿重装）：{workspace.conda_env_dir}
- 环境 python：{python_bin}
</项目元信息>

<用户选择的作业参数>
{form_text}
</用户选择的作业参数>

<当前运行目录树（根目录就是命令执行位置）>
{_project_tree(run_dir)}
</当前运行目录树>

<环境已安装的包（部分）>
{installed_text}
</环境已安装的包>

<用户需求记录（仅用于理解需求；其中的路径只有出现在当前运行目录树中才可使用）>
{_trim_text(notes_text.strip(), 3000) if notes_text.strip() else "（无）"}
</用户需求记录>

<当前运行目录内可直接阅读的文本文件内容>
{_collect_readable_text_files(run_dir)}
</当前运行目录内可直接阅读的文本文件内容>
"""
    return _trim_text(context, MAX_CONTEXT_TEXT_CHARS)


def _extract_bash_body(raw: str) -> str:
    """从 LLM 回复里提取纯命令正文：剥围栏、去头部行、去重复的环境准备行。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    fences = re.findall(r"```(?:bash|shell|sh)?\s*(.*?)```", text, flags=re.S)
    if fences:
        text = max(fences, key=len).strip()
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#SBATCH") or stripped.startswith("#!"):
            continue
        # 已由锁定区固定处理的内容，防止模型重复输出；
        # 注意不过滤 cd——正文中 cd 进子目录是合法需求
        if re.match(r"^(source\s+\S*conda\.sh|conda\s+activate\b|conda\s+run\b)", stripped):
            continue
        if re.match(r"^mkdir\s+-p\s+(logs|runs)\b", stripped):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


_COMMAND_PATH_FLAGS = {
    "--config": "配置文件",
    "--resume": "断点文件",
    "--checkpoint": "检查点文件",
    "--weights": "权重文件",
}


def _literal_command_path(token: str) -> Optional[str]:
    """返回可在登录节点校验的字面路径；变量、通配符和 URI 留到运行时处理。"""
    value = str(token or "").strip().rstrip(";")
    if (
        not value
        or value == "-"
        or "://" in value
        or any(char in value for char in "$*?[]{}")
    ):
        return None
    return value


def _logical_shell_lines(command: str):
    """合并 Bash 的反斜杠续行，并保留每条逻辑命令的起始物理行号。"""
    current = ""
    start_line = 1
    continuing = False
    for line_no, line in enumerate(str(command or "").splitlines(), start=1):
        if not continuing:
            current = line
            start_line = line_no
        else:
            current += line

        trailing_backslashes = len(current) - len(current.rstrip("\\"))
        if trailing_backslashes % 2 == 1:
            current = current[:-1]
            continuing = True
            continue

        yield start_line, current
        current = ""
        continuing = False

    if continuing:
        # 保留未闭合的反斜杠，让 shlex 给出真实语法错误。
        yield start_line, current + "\\"


def _command_path_errors(command: str, run_dir: Path) -> list[str]:
    """检查命令正文中的入口脚本和关键输入文件是否基于 run_dir 存在。"""
    errors: list[str] = []

    def add_path(path_text: str, label: str, line_no: int) -> None:
        literal = _literal_command_path(path_text)
        if literal is None:
            return
        path = Path(literal)
        resolved = path if path.is_absolute() else run_dir / path
        if resolved.exists():
            return

        normalized = literal.replace("\\", "/")
        duplicate_prefix = f"{run_dir.name}/"
        if not path.is_absolute() and normalized.startswith(duplicate_prefix):
            shorter = normalized[len(duplicate_prefix):]
            if shorter and (run_dir / shorter).exists():
                errors.append(
                    f"第 {line_no} 行的{label} {literal!r} 重复包含运行目录名；"
                    f"应改为 {shorter!r}"
                )
                return
        errors.append(
            f"第 {line_no} 行的{label} {literal!r} 在运行目录 {run_dir} 下不存在"
        )

    for line_no, line in _logical_shell_lines(command):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped, comments=True, posix=True)
        except ValueError as exc:
            errors.append(f"第 {line_no} 行 shell 语法无法解析：{exc}")
            continue
        if not tokens:
            continue

        # 检查所有路径 token 是否错误地再次带上了当前运行目录名。
        prefix = f"{run_dir.name}/"
        for token in tokens:
            literal = _literal_command_path(token)
            if literal is None or Path(literal).is_absolute():
                continue
            normalized = literal.replace("\\", "/")
            if normalized.startswith(prefix):
                shorter = normalized[len(prefix):]
                if shorter and not (run_dir / normalized).exists() and (run_dir / shorter).exists():
                    message = (
                        f"第 {line_no} 行路径 {literal!r} 重复包含运行目录名；"
                        f"应改为 {shorter!r}"
                    )
                    if message not in errors:
                        errors.append(message)

        # Python 的第一个非解释器选项参数是入口脚本；python -m/-c 不按文件校验。
        python_index = next(
            (
                index
                for index, token in enumerate(tokens)
                if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(token).name)
            ),
            None,
        )
        if python_index is not None:
            index = python_index + 1
            while index < len(tokens):
                token = tokens[index]
                if token in {"-m", "-c"}:
                    break
                if token in {"-W", "-X"}:
                    index += 2
                    continue
                if token == "--" and index + 1 < len(tokens):
                    add_path(tokens[index + 1], "Python 入口脚本", line_no)
                    break
                if token.startswith("-"):
                    index += 1
                    continue
                add_path(token, "Python 入口脚本", line_no)
                break

        # bash/sh 脚本入口也必须存在。
        shell_index = next(
            (
                index
                for index, token in enumerate(tokens)
                if Path(token).name in {"bash", "sh"}
            ),
            None,
        )
        if shell_index is not None:
            index = shell_index + 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            if index < len(tokens):
                add_path(tokens[index], "Shell 入口脚本", line_no)

        # 配置、权重和断点属于运行前必须存在的输入。
        for index, token in enumerate(tokens):
            for flag, label in _COMMAND_PATH_FLAGS.items():
                if token == flag and index + 1 < len(tokens):
                    add_path(tokens[index + 1], label, line_no)
                elif token.startswith(flag + "="):
                    add_path(token.split("=", 1)[1], label, line_no)

    deduplicated: list[str] = []
    seen: set[tuple[str, str]] = set()
    for message in errors:
        match = re.search(r"第\s*(\d+)\s*行.*?('.*?')", message)
        key = (match.group(1), match.group(2)) if match else ("", message)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(message)
    return deduplicated


def _build_job_prelude(workspace: ProjectWorkspace, run_dir: Path) -> str:
    """生成服务端锁定的工作目录与 Conda 前导。"""
    conda_sh = _conda_sh_path()
    return "\n".join([
        "set -euo pipefail",
        "# 运行目录（服务端锁定）",
        f"cd {shlex.quote(str(run_dir))}",
        "mkdir -p logs",
        "",
        "# 激活项目 Conda 环境（服务端锁定）",
        "set +u",
        f"source {conda_sh}",
        f"conda activate {shlex.quote(str(workspace.conda_env_dir))}",
        "set -u",
    ])


def _validated_job_draft(raw: dict) -> dict:
    """校验 Web/Agent 共用的结构化作业草稿和 Slurm 授权。"""
    project_name = str(raw.get("project_name") or "").strip()
    command = str(raw.get("command") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    job_name = str(raw.get("job_name") or raw.get("name") or "").strip()
    partition = str(raw.get("partition") or "").strip()
    account = str(raw.get("account") or "").strip()
    qos = str(raw.get("qos") or "").strip()
    subdir = str(raw.get("subdir") or "").strip().strip("/")

    if not project_name:
        raise FileTransferError("必须在一个项目中提交作业")
    if not command:
        raise FileTransferError("作业命令不能为空")
    if len(command) > 100_000:
        raise FileTransferError("作业命令过长（最多 100000 字符）")
    if any(
        line.strip().startswith(("#!", "#SBATCH"))
        for line in command.splitlines()
    ):
        raise FileTransferError(
            "命令正文不能包含 #! 或 #SBATCH；作业头、目录、环境和日志由受控后端生成"
        )
    if not job_name or not partition or not account or not qos:
        raise FileTransferError("作业名、计费账户、分区和 QoS 均不能为空")

    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "", job_name).strip(".-")
    if not safe_name:
        raise FileTransferError("作业名只能包含字母、数字、点、下划线和短横线")

    try:
        nodes = int(raw.get("nodes", 1))
        cpus_per_task = int(raw.get("cpus_per_task", 1))
        gpus_per_node = int(raw.get("gpus_per_node", 0))
        memory_mb = int(raw.get("memory_mb", 16384))
        time_limit = int(raw.get("time_limit", 240))
    except (TypeError, ValueError):
        raise FileTransferError("节点、CPU、GPU、内存和时限必须是整数")

    if not 1 <= nodes <= 64:
        raise FileTransferError("节点数必须在 1 到 64 之间")
    if not 1 <= cpus_per_task <= 512:
        raise FileTransferError("每任务 CPU 核数必须在 1 到 512 之间")
    if not 0 <= gpus_per_node <= 16:
        raise FileTransferError("每节点 GPU 数必须在 0 到 16 之间")
    if not 128 <= memory_mb <= 8 * 1024 * 1024:
        raise FileTransferError("每节点内存必须在 128 MB 到 8 TB 之间")
    if not 1 <= time_limit <= 30 * 24 * 60:
        raise FileTransferError("作业时限必须在 1 分钟到 30 天之间")
    if gpus_per_node and partition.startswith("CPU-"):
        raise FileTransferError(f"CPU 分区 {partition} 不能申请 GPU")

    accounts, user_qos = _user_slurm_accounts()
    if account not in accounts:
        raise FileTransferError(f"当前用户未获授权使用计费账户 {account}")
    partition_entry = next(
        (item for item in _partition_permissions() if item["partition"] == partition),
        None,
    )
    if partition_entry is None:
        raise FileTransferError(f"分区不存在或当前无法读取：{partition}")
    if partition_entry["accounts"] and account not in partition_entry["accounts"]:
        raise FileTransferError(f"账户 {account} 无权使用分区 {partition}")
    if qos not in user_qos:
        raise FileTransferError(f"当前用户未获授权使用 QoS {qos}")
    if partition_entry["qos"] and qos not in partition_entry["qos"]:
        raise FileTransferError(f"分区 {partition} 不允许 QoS {qos}")
    max_nodes = partition_entry.get("max_nodes")
    if max_nodes is not None and nodes > max_nodes:
        raise FileTransferError(f"分区 {partition} 最多允许 {max_nodes} 个节点")

    try:
        qos_limits = _qos_limits_from_rest()
    except Exception:
        logger.exception("提交前读取 QoS 上限失败，使用静态表")
        qos_limits = {}
    limits = qos_limits.get(qos) or STATIC_QOS_LIMITS.get(qos) or {}
    requested_cpu = nodes * cpus_per_task
    requested_gpu = nodes * gpus_per_node
    requested_memory = nodes * memory_mb
    if limits.get("cpu") is not None and requested_cpu > int(limits["cpu"]):
        raise FileTransferError(f"CPU 申请 {requested_cpu} 核超过 QoS 上限 {limits['cpu']} 核")
    if limits.get("gpu") is not None and requested_gpu > int(limits["gpu"]):
        raise FileTransferError(f"GPU 申请 {requested_gpu} 卡超过 QoS 上限 {limits['gpu']} 卡")
    if limits.get("mem_mb") is not None and requested_memory > int(limits["mem_mb"]):
        raise FileTransferError(
            f"内存申请 {requested_memory} MB 超过 QoS 上限 {limits['mem_mb']} MB"
        )
    if limits.get("wall_minutes") is not None and time_limit > int(limits["wall_minutes"]):
        raise FileTransferError(
            f"时限 {time_limit} 分钟超过 QoS 上限 {limits['wall_minutes']} 分钟"
        )

    return {
        "project_name": project_name,
        "subdir": subdir,
        "command": command,
        "job_name": safe_name,
        "partition": partition,
        "account": account,
        "qos": qos,
        "nodes": nodes,
        "cpus_per_task": cpus_per_task,
        "gpus_per_node": gpus_per_node,
        "memory_mb": memory_mb,
        "time_limit": time_limit,
        "source": str(raw.get("source") or "web"),
    }


def _job_id_from_response(result: dict):
    if not isinstance(result, dict):
        return None
    for key in ("job_id", "jobid", "id"):
        if result.get(key) is not None:
            return result[key]
    nested = result.get("result")
    if isinstance(nested, dict):
        return _job_id_from_response(nested)
    return None


def _tres_evidence(value, parent_key: str = "") -> list[str]:
    """只收集 TRES/GRES 相关字段，避免普通 GPU 文本造成误判。"""
    evidence: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if "tres" in key_text or "gres" in key_text:
                evidence.append(f"{key}={item}")
            evidence.extend(_tres_evidence(item, key_text))
    elif isinstance(value, list):
        for item in value:
            evidence.extend(_tres_evidence(item, parent_key))
    return evidence


def _requested_gpu_counts(value, inside_requested: bool = False) -> list[int]:
    """兼容 job.tres.requested 列表与 tres_per_node 字符串两种响应。"""
    counts: list[int] = []
    if isinstance(value, dict):
        if inside_requested:
            tres_type = str(value.get("type") or "").lower()
            tres_name = str(value.get("name") or "").lower()
            if tres_type == "gres" and tres_name == "gpu":
                try:
                    counts.append(int(value.get("count")))
                except (TypeError, ValueError):
                    pass
        for key, item in value.items():
            key_text = str(key).lower()
            child_requested = inside_requested or key_text == "requested"
            if key_text == "tres_per_node" and isinstance(item, str):
                match = re.search(r"gres/gpu\s*[:=]\s*(\d+)", item.lower())
                if match:
                    counts.append(int(match.group(1)))
            counts.extend(_requested_gpu_counts(item, child_requested))
    elif isinstance(value, list):
        for item in value:
            counts.extend(_requested_gpu_counts(item, inside_requested))
    return counts


def _verify_submitted_resources(
    client: SlurmClient,
    job_id,
    gpus_per_node: int,
    nodes: int,
) -> dict:
    if not gpus_per_node:
        return {
            "status": "verified",
            "gpu_requested": 0,
            "message": "已按结构化 REST 字段提交 CPU 作业",
        }
    if job_id is None:
        return {
            "status": "pending",
            "gpu_requested": gpus_per_node,
            "message": "作业已提交，但未返回 job_id，GPU 资源尚无法核验",
        }
    try:
        job_data = client.get_job(int(job_id))
    except Exception as exc:
        logger.warning("作业 %s 提交后资源核验暂不可用: %s", job_id, exc)
        return {
            "status": "pending",
            "gpu_requested": gpus_per_node,
            "message": "作业已提交，但 Slurm 暂未返回详情，不能确认 GPU 已分配",
        }

    evidence = _tres_evidence(job_data)
    gpu_counts = _requested_gpu_counts(job_data)
    requested_total = gpus_per_node * nodes
    if any(count in {gpus_per_node, requested_total} for count in gpu_counts):
        return {
            "status": "verified",
            "gpu_requested": gpus_per_node,
            "message": f"Slurm 已确认每节点申请 {gpus_per_node} 张 GPU",
            "evidence": evidence[:8],
        }
    return {
        "status": "mismatch",
        "gpu_requested": gpus_per_node,
        "message": "作业已提交，但 Slurm 返回的请求资源中没有匹配的 GPU；请勿按 GPU 作业运行",
        "evidence": evidence[:8],
    }


def _build_controlled_job_script(
    draft: dict, workspace: ProjectWorkspace, run_dir: Path
) -> str:
    """按受控提交规则生成完整脚本；提交和模板保存共用此实现。"""
    path_errors = _command_path_errors(draft["command"], run_dir)
    if path_errors:
        raise FileTransferError(
            "命令路径校验失败：" + "；".join(path_errors[:6])
        )

    header = [
        "#!/bin/bash",
        f"#SBATCH --job-name={draft['job_name']}",
        f"#SBATCH --partition={draft['partition']}",
        f"#SBATCH --account={draft['account']}",
        f"#SBATCH --qos={draft['qos']}",
        f"#SBATCH --nodes={draft['nodes']}",
        f"#SBATCH --cpus-per-task={draft['cpus_per_task']}",
        f"#SBATCH --mem={draft['memory_mb']}M",
        f"#SBATCH --time={draft['time_limit']}",
        "#SBATCH --output=logs/%x-%j.out",
        "#SBATCH --error=logs/%x-%j.err",
    ]
    if draft["gpus_per_node"]:
        header.append(f"#SBATCH --gres=gpu:{draft['gpus_per_node']}")

    sections = [
        "\n".join(header),
        _build_job_prelude(workspace, run_dir),
    ]
    if draft["gpus_per_node"]:
        sections.append("\n".join([
            "# GPU 资源预检（由服务端注入）",
            'echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-<unset>}"',
            'srun --nodes=1 --ntasks=1 bash -c \'test -n "${CUDA_VISIBLE_DEVICES:-}" || { echo "GPU requested but CUDA_VISIBLE_DEVICES is empty" >&2; exit 1; }\'',
            "srun --nodes=1 --ntasks=1 nvidia-smi -L",
        ]))
    sections.append("\n".join([
        "# --- SLURM-AGENT COMMAND BEGIN ---",
        draft["command"],
        "# --- SLURM-AGENT COMMAND END ---",
    ]))
    return "\n\n".join(sections).rstrip() + "\n"


def _submit_controlled_job(raw_draft: dict) -> dict:
    """Web 与 Agent 唯一的项目作业提交实现。"""
    draft = _validated_job_draft(raw_draft)
    workspace = ensure_project_workspace(draft["project_name"])
    run_dir = _resolve_run_dir(workspace.project_dir, draft["subdir"])
    script = _build_controlled_job_script(draft, workspace, run_dir)
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    script_path = logs_dir / f"job-{draft['job_name']}.sh"
    script_path.write_text(script, encoding="utf-8")

    stdout_path = str(logs_dir / f"{draft['job_name']}-%j.out")
    stderr_path = str(logs_dir / f"{draft['job_name']}-%j.err")
    client = SlurmClient()
    result = client.submit_job(
        script=script,
        partition=draft["partition"],
        name=draft["job_name"],
        nodes=draft["nodes"],
        time_limit=draft["time_limit"],
        account=draft["account"],
        qos=draft["qos"],
        cpus_per_task=draft["cpus_per_task"],
        gpus_per_node=draft["gpus_per_node"],
        memory_mb=draft["memory_mb"],
        working_directory=str(run_dir),
        standard_output=stdout_path,
        standard_error=stderr_path,
    )
    job_id = _job_id_from_response(result)

    # 登记作业供心跳监控：完成/失败时前端会收到右下角通知（Web 与 Agent 共用此路径）
    if job_id is not None:
        try:
            _register_watched_job(
                job_id, draft["job_name"], workspace.project_name,
                draft["subdir"], logs_dir,
            )
        except Exception:
            logger.exception("登记作业监控失败（不影响提交结果）")

    verification = _verify_submitted_resources(
        client, job_id, draft["gpus_per_node"], draft["nodes"]
    )
    return {
        "status": "ok",
        "source": draft["source"],
        "project_name": workspace.project_name,
        "project_dir": str(workspace.project_dir),
        "run_dir": str(run_dir),
        "subdir": draft["subdir"],
        "conda_env_dir": str(workspace.conda_env_dir),
        "script_path": str(script_path),
        "job_id": job_id,
        "requested_resources": {
            "account": draft["account"],
            "qos": draft["qos"],
            "partition": draft["partition"],
            "nodes": draft["nodes"],
            "cpus_per_task": draft["cpus_per_task"],
            "gpus_per_node": draft["gpus_per_node"],
            "memory_mb_per_node": draft["memory_mb"],
            "time_limit_minutes": draft["time_limit"],
        },
        "resource_verification": verification,
        "slurm_response": result,
    }


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------


@app.post("/chat")
async def chat(req: ChatRequest):
    """
    发送消息给智能体，SSE 流式返回。

    流式事件类型：
      - tool_start: 开始执行工具 {"tool": "read_job_log", "args": {...}}
      - tool_end:   工具执行完成 {"tool": "read_job_log", "result": "..."}
      - text:       LLM 文本回复片段
      - done:       对话完成
      - error:      出错
    """
    user_message = req.message.strip()
    if not user_message:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)
    project_name = ""
    subdir = (req.subdir or "").strip().strip("/")
    if req.project_name.strip():
        try:
            project_name, project_dir, _ = project_workspace(req.project_name)
            _resolve_run_dir(project_dir, subdir)
        except FileTransferError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _append_chat_history(project_name, "user", user_message, subdir)

    async def event_stream():
        try:
            ag = get_agent(project_name, subdir)
            ag.messages.append({"role": "user", "content": user_message})
            final_reply = ""

            turn = 0
            max_tool_turns = getattr(ag, "max_turns", 20)
            while turn < max_tool_turns:
                turn += 1
                logger.info("第 %d 轮 LLM 调用...", turn)

                # 调用 LLM
                try:
                    response = ag.llm.chat(
                        messages=ag.messages,
                        tools=TOOL_DEFINITIONS,
                    )
                except Exception as e:
                    error_text = f"LLM调用失败: {e}"
                    if project_name:
                        _append_chat_history(project_name, "ai", error_text, subdir)
                    yield f"data: {json.dumps({'type': 'error', 'content': error_text}, ensure_ascii=False)}\n\n"
                    return

                choice = response.choices[0]
                message = choice.message

                # 情况 A：模型返回 tool_calls
                if message.tool_calls:
                    # 追加 assistant 消息（含 tool_calls）
                    tool_calls_data = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ]
                    ag.messages.append({
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": tool_calls_data,
                    })

                    # 逐个执行工具
                    for tc in message.tool_calls:
                        tool_name = tc.function.name
                        try:
                            arguments = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            arguments = {}

                        try:
                            result_str = ag.executor.execute(tool_name, arguments)
                        except Exception as e:
                            result_str = f"工具执行出错: {e}"

                        # 追加 tool 结果消息
                        ag.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_str,
                        })

                    continue  # 继续循环

                # 情况 B：模型直接返回文本 → 流式输出
                reply = message.content or ""
                final_reply = reply
                ag.messages.append({"role": "assistant", "content": reply})

                # 按句子/段落切分，模拟流式输出
                # 简单按字符块发送
                chunk_size = 20
                for i in range(0, len(reply), chunk_size):
                    chunk = reply[i:i + chunk_size]
                    yield f"data: {json.dumps({'type': 'text', 'content': chunk}, ensure_ascii=False)}\n\n"

                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                if project_name:
                    _append_chat_history(project_name, "ai", final_reply, subdir)
                return

            # 超过最大轮数
            error_text = "超过最大工具调用轮数，请简化问题后重试。"
            if project_name:
                _append_chat_history(project_name, "ai", error_text, subdir)
            yield f"data: {json.dumps({'type': 'error', 'content': error_text}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.exception("处理请求异常")
            error_text = f"服务异常: {e}"
            if project_name:
                _append_chat_history(project_name, "ai", error_text, subdir)
            yield f"data: {json.dumps({'type': 'error', 'content': error_text}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/reset", response_model=ResetResponse)
async def reset(req: Request):
    """重置对话历史。"""
    global agent
    project_name = ""
    subdir = ""
    try:
        body = await req.json()
        project_name = str(body.get("project_name") or "").strip()
        subdir = str(body.get("subdir") or "").strip().strip("/")
    except Exception:
        project_name = ""
        subdir = ""

    if project_name:
        safe_project_name, project_dir, _ = project_workspace(project_name)
        _resolve_run_dir(project_dir, subdir)
        cache_key = f"{safe_project_name}/{subdir}" if subdir else safe_project_name
        project_agents.pop(cache_key, None)
        _write_chat_history(safe_project_name, [], subdir)
        return ResetResponse(status="ok")

    if agent:
        agent.reset()
    else:
        agent = AgentLoop()
    return ResetResponse(status="ok")


@app.post("/api/slurm/refresh")
def slurm_refresh():
    """Refresh SLURM_JWT on the login node and verify Slurm REST API access."""
    try:
        token = refresh_slurm_token()
        diag = SlurmClient(auto_refresh_token=False).get_diag()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    return {
        "status": "ok",
        "token_preview": token_preview(token),
        "diag_keys": sorted(diag.keys())[:8] if isinstance(diag, dict) else [],
    }


def _cli_env() -> dict:
    """Slurm CLI 子进程环境：剔除 SLURM_JWT。

    进程内 REST 客户端刷新 token 后会把 SLURM_JWT 写进 os.environ
    （core/slurm_client.py），而 sacctmgr/scontrol 等 CLI 也会读这个变量，
    拿到 REST 的 JWT 后改用 JWT 认证 slurmdbd 持久连接，报
    "Protocol authentication error"。CLI 必须用默认 MUNGE 认证。
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("SLURM_JWT")}


def _user_slurm_accounts() -> tuple[set, set]:
    """sacctmgr -n -P 读取当前用户的账户与 QoS 授权。

    必须加 -P（parsable）：默认表格输出会把逗号分隔的 QoS 列截断，
    拿不到完整 QoS 名（如 qos_p107-rtx5090 会被截断成 qos_p107-...）。
    """
    accounts: set = set()
    qos_set: set = set()
    user = getpass.getuser()
    result = subprocess.run(
        ["sacctmgr", "-n", "-P", "show", "assoc",
         f"user={user}", "format=user,account,partition,qos"],
        capture_output=True, text=True, timeout=20, env=_cli_env(),
    )
    if result.returncode != 0 or not result.stdout.strip():
        # sacctmgr 连不上 slurmdbd 时 rc 非 0、stdout 为空，错误信息在 stderr，
        # 必须与「用户真的没有关联」区分开，否则前端拿到误导性提示
        raise RuntimeError(
            f"sacctmgr 执行异常 rc={result.returncode}: {result.stderr.strip()[:300] or 'stdout 为空'}"
        )
    logger.info("sacctmgr user=%s 输出 %d 行", user, len(result.stdout.splitlines()))
    for line in result.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 4 or not parts[1]:
            continue
        accounts.add(parts[1])
        qos_set.update(q for q in parts[3].split(",") if q and q.lower() != "null")
    return accounts, qos_set


def _partition_permissions() -> list[dict]:
    """scontrol show part 解析每个分区的 AllowAccounts / AllowQos / MaxNodes。"""
    partitions: list[dict] = []
    result = subprocess.run(
        ["scontrol", "show", "part"],
        capture_output=True, text=True, timeout=20, env=_cli_env(),
    )
    current: Optional[dict] = None
    for line in result.stdout.splitlines():
        name_match = re.match(r"PartitionName=(\S+)", line.strip())
        if name_match:
            if current:
                partitions.append(current)
            current = {"partition": name_match.group(1), "accounts": [], "qos": [], "max_nodes": None}
            continue
        if current is None:
            continue
        acc_match = re.search(r"AllowAccounts=(\S+)", line)
        if acc_match and acc_match.group(1) not in ("ALL", "__NONE__"):
            current["accounts"] = acc_match.group(1).split(",")
        qos_match = re.search(r"AllowQos=(\S+)", line)
        if qos_match and qos_match.group(1) not in ("ALL", "__NONE__"):
            current["qos"] = qos_match.group(1).split(",")
        nodes_match = re.search(r"MaxNodes=(\d+|UNLIMITED)", line)
        if nodes_match and nodes_match.group(1).isdigit():
            current["max_nodes"] = int(nodes_match.group(1))
    if current:
        partitions.append(current)
    return partitions


def _qos_limits_from_rest() -> dict:
    """REST slurmdb /qos：每个 QoS 的 CPU/GPU/内存/墙钟上限。

    对应 docs/overview/resources.md 的「平台内置 QOS 方案示例」表，
    但为实时值（该文档明确说明表为快照、以平台当前为准）。
    """
    limits: dict = {}
    data = SlurmClient().get_qos()
    for q in data.get("qos", []):
        name = q.get("name") or ""
        try:
            tres = q["limits"]["max"]["tres"]["per"]["user"]
            wall = q["limits"]["max"]["wall_clock"]["per"]["job"]
        except (KeyError, TypeError):
            continue
        entry = {
            "cpu": next((t.get("count") for t in tres if t.get("type") == "cpu"), None),
            "gpu": next((t.get("count") for t in tres
                         if t.get("type") == "gres" and t.get("name") == "gpu"), None),
            "mem_mb": next((t.get("count") for t in tres if t.get("type") == "mem"), None),
            "wall_minutes": wall.get("number") if wall.get("set") else None,
        }
        if any(v is not None for v in entry.values()):
            limits[name] = entry
    return limits


@app.get("/api/slurm/submit-options")
async def slurm_submit_options():
    """账户→分区→QoS 三级可选组合及每个 QoS 的资源上限（全部来自 Slurm 实时数据）。

    组合推导：账户来自 sacctmgr 用户关联；分区要求账户在分区 AllowAccounts
    白名单内；QoS 取分区 AllowQos 与用户授权 QoS 的交集，保证菜单里每一项
    都是集群授权验证过的组合，不让用户随便填。
    """
    try:
        accounts, user_qos = _user_slurm_accounts()
    except Exception as e:
        logger.exception("sacctmgr 读取用户授权失败")
        return JSONResponse({"error": f"读取账户/QoS 授权失败：{e}"}, status_code=502)
    if not accounts:
        return JSONResponse({"error": "当前用户没有可用的 Slurm 账户关联"}, status_code=502)

    try:
        partitions = _partition_permissions()
    except Exception:
        logger.exception("scontrol show part 失败")
        return JSONResponse({"error": "读取分区信息失败（scontrol 不可用）"}, status_code=502)

    options = []
    for account in sorted(accounts):
        parts = []
        for p in partitions:
            if account not in p["accounts"]:
                continue
            allowed_qos = [q for q in p["qos"] if q in user_qos] if p["qos"] else sorted(user_qos)
            if not allowed_qos:
                continue
            parts.append({
                "partition": p["partition"],
                "qos": allowed_qos,
                "max_nodes": p["max_nodes"],
            })
        if parts:
            options.append({"account": account, "partitions": parts})

    if not options:
        return JSONResponse({"error": "当前用户没有可用的 账户/分区/QoS 组合"}, status_code=502)

    try:
        qos_limits = _qos_limits_from_rest()
    except Exception:
        logger.exception("REST 读取 QoS 上限失败，回退静态表")
        qos_limits = {}
    for name, entry in STATIC_QOS_LIMITS.items():
        qos_limits.setdefault(name, entry)

    return {"status": "ok", "options": options, "qos_limits": qos_limits}


@app.get("/api/models")
async def models(refresh: bool = False):
    """Return DeepSeek/GLM models available to the current LLM_API_KEY."""
    try:
        config = refresh_model_config() if refresh else ensure_model_config_current()
    except Exception as e:
        return JSONResponse({"error": f"获取模型列表失败: {e}"}, status_code=502)
    return {"status": "ok", **config}


@app.post("/api/models/refresh")
async def models_refresh():
    """Force refresh model list for the current LLM_API_KEY."""
    try:
        config = refresh_model_config()
    except Exception as e:
        return JSONResponse({"error": f"刷新模型列表失败: {e}"}, status_code=502)
    return {"status": "ok", **config}


@app.post("/api/models/select")
async def models_select(req: ModelSelectRequest):
    """Persist selected model and reset in-memory agent instances."""
    global agent, project_agents
    try:
        config = set_selected_model(req.model)
    except Exception as e:
        return JSONResponse({"error": f"切换模型失败: {e}"}, status_code=400)
    agent = None
    project_agents = {}
    return {"status": "ok", **config}


@app.get("/api/dashboard")
async def dashboard():
    """Return a compact resource and job snapshot for the console UI."""
    client = SlurmClient()
    errors: list[str] = []
    nodes: list[dict] = []
    jobs: list[dict] = []
    diag: dict = {}

    try:
        node_data = client.get_nodes()
        raw_nodes = node_data.get("nodes", []) if isinstance(node_data, dict) else []
        nodes = [_summarize_node(node) for node in raw_nodes if isinstance(node, dict)]
    except Exception as e:
        errors.append(f"节点数据获取失败: {e}")

    try:
        job_data = client.list_jobs()
        raw_jobs = job_data.get("jobs", []) if isinstance(job_data, dict) else []
        jobs = [_summarize_job(job) for job in raw_jobs if isinstance(job, dict)]
    except Exception as e:
        errors.append(f"作业数据获取失败: {e}")

    try:
        diag = client.get_diag()
    except Exception as e:
        errors.append(f"诊断数据获取失败: {e}")

    total_cpus = sum(node["cpus"] for node in nodes)
    alloc_cpus = sum(node["alloc_cpus"] for node in nodes)
    current_user = getpass.getuser()
    my_jobs = [job for job in jobs if str(job.get("user")) == current_user]

    return {
        "status": "ok" if not errors else "partial",
        "errors": errors,
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "node_count": len(nodes),
            "job_count": len(jobs),
            "current_user": current_user,
            "my_job_count": len(my_jobs),
            "total_cpus": total_cpus,
            "alloc_cpus": alloc_cpus,
            "node_states": _count_by_state(nodes),
            "job_states": _count_by_state(jobs),
            "my_job_states": _count_by_state(my_jobs),
        },
        "nodes": nodes[:120],
        "jobs": jobs[:80],
        "my_jobs": my_jobs[:80],
        "diag_keys": sorted(diag.keys())[:8] if isinstance(diag, dict) else [],
    }


@app.get("/api/jobs/history")
def jobs_history(days: int = 30, limit: int = 50):
    """查询当前用户的历史作业（slurmdbd），默认最近 30 天，按提交时间倒序。"""
    try:
        client = SlurmClient()
        now = datetime.now()
        params = {
            "users": getpass.getuser(),
            "start_time": (now - timedelta(days=max(1, days))).strftime("%Y-%m-%d"),
            "end_time": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
        }
        raw_jobs = client.get_jobs_history(params=params).get("jobs", [])
        items = []
        for job in raw_jobs:
            if not isinstance(job, dict):
                continue
            times = job.get("time") or {}
            states = (job.get("state") or {}).get("current") or []
            items.append({
                "id": job.get("job_id") or "-",
                "name": job.get("name") or "-",
                "state": _state_text(states[0] if states else None),
                "partition": job.get("partition") or "-",
                "submit_time": _ts_seconds(times.get("submission")),
                "start_time": _ts_seconds(times.get("start")),
                "end_time": _ts_seconds(times.get("end")),
                "elapsed_seconds": max(0, _number(times.get("elapsed"))),
            })
        items.sort(key=lambda item: item["submit_time"], reverse=True)
        return {"status": "ok", "days": max(1, days), "count": len(items), "jobs": items[:limit]}
    except Exception as e:
        return JSONResponse({"error": f"查询历史作业失败: {e}"}, status_code=500)


@app.get("/api/projects")
async def list_projects():
    """List first-level project folders under the configured projects base."""
    try:
        projects_base = Path(get_remote_projects_base()).expanduser().resolve()
        projects_base.mkdir(parents=True, exist_ok=True)
        items = []
        for path in projects_base.iterdir():
            if not path.is_dir() or path.name.startswith("."):
                continue
            stat = path.stat()
            history_path = path / ".slurm-agent" / "chat-history.json"
            notes_path = path / PROJECT_NOTES_FILENAME
            subdirs = _project_subdirs(path)
            # 项目活跃时间 = 项目根/各小文件夹最新修改时间（小文件夹里的动静也算）
            latest = stat.st_mtime
            for sub in subdirs:
                try:
                    latest = max(latest, (path / sub).stat().st_mtime)
                except OSError:
                    continue
            items.append({
                "name": path.name,
                "path": str(path),
                "conda_env_dir": str(path / ".slurm-agent" / "conda-env"),
                "updated_at": datetime.fromtimestamp(latest).isoformat(timespec="seconds"),
                "has_chat": history_path.exists(),
                "has_notes": notes_path.exists(),
                "subdirs": subdirs,
            })
        items.sort(key=lambda item: item["updated_at"], reverse=True)
    except Exception as e:
        return JSONResponse({"error": f"读取作业目录列表失败: {e}"}, status_code=500)
    return {"status": "ok", "base_dir": str(projects_base), "projects": items}


# ---------------------------------------------------------------------------
# 帮助文档：内置于仓库 docs/ 目录，供前端阅读器渲染
# ---------------------------------------------------------------------------
DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs" / "docs-main" / "docs"
DOCS_DIR_ORDER = {"overview": 0, "basics": 1, "guides": 2, "reference": 3}


def _strip_docs_frontmatter(text: str) -> str:
    """去掉 docs 页面开头的 YAML frontmatter（--- ... --- 块）。"""
    if not text.startswith("---"):
        return text
    lines = text.splitlines()
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1:]).lstrip("\n")
    return text


def _docs_title(raw_text: str, fallback: str) -> str:
    """取文档标题：正文第一个 # 标题，否则回退文件名。"""
    for line in _strip_docs_frontmatter(raw_text).splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _docs_subtree(directory: Path) -> list[dict]:
    """递归构建 docs 目录树，只收 .md 文件；目录按固定顺序、文件按名称排序。"""
    nodes: list[dict] = []
    try:
        entries = sorted(
            directory.iterdir(),
            key=lambda p: (
                0 if p.is_dir() else 1,
                DOCS_DIR_ORDER.get(p.name, 99) if p.is_dir() else 0,
                p.name,
            ),
        )
    except OSError:
        return nodes
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "assets":
            continue
        if entry.is_dir():
            children = _docs_subtree(entry)
            if children:
                nodes.append({"name": entry.name, "type": "dir", "children": children})
        elif entry.suffix == ".md":
            rel = entry.relative_to(DOCS_ROOT).as_posix()
            try:
                raw = entry.read_text(encoding="utf-8")
            except OSError:
                continue
            nodes.append({
                "name": entry.stem,
                "title": _docs_title(raw, entry.stem),
                "type": "file",
                "path": rel,
            })
    return nodes


@app.get("/api/docs/tree")
def docs_tree():
    """返回内置帮助文档的目录树。"""
    if not DOCS_ROOT.is_dir():
        return JSONResponse({"error": "文档目录不存在"}, status_code=500)
    return {"status": "ok", "root": str(DOCS_ROOT), "tree": _docs_subtree(DOCS_ROOT)}


@app.get("/api/docs/content")
def docs_content(path: str):
    """返回一篇文档的正文（已去 frontmatter）。路径必须落在 docs 根内。"""
    if not path or ".." in path.split("/") or path.startswith("/"):
        return JSONResponse({"error": "非法路径"}, status_code=400)
    target = (DOCS_ROOT / path).resolve()
    try:
        target.relative_to(DOCS_ROOT.resolve())
    except ValueError:
        return JSONResponse({"error": "非法路径"}, status_code=400)
    if target.suffix != ".md" or not target.is_file():
        return JSONResponse({"error": "文档不存在"}, status_code=404)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as e:
        return JSONResponse({"error": f"读取文档失败: {e}"}, status_code=500)
    return {
        "status": "ok",
        "path": path,
        "title": _docs_title(raw, target.stem),
        "content": _strip_docs_frontmatter(raw),
    }


@app.get("/api/projects/chat")
async def project_chat_history(project_name: str, subdir: str = ""):
    """Return display chat history for one project session (project root or a dataset subdir)."""
    try:
        safe_project_name, project_dir, _ = project_workspace(project_name)
        messages = _read_chat_history(safe_project_name, subdir)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {
        "status": "ok",
        "project_name": safe_project_name,
        "project_dir": str(project_dir),
        "subdir": (subdir or "").strip().strip("/"),
        "messages": messages,
    }


@app.post("/api/projects/chat")
async def append_project_chat(req: ProjectChatAppendRequest):
    """Append a display message to one project session history."""
    try:
        safe_project_name, _, _ = project_workspace(req.project_name)
        role = "ai" if req.role == "ai" else "user"
        _append_chat_history(safe_project_name, role, req.content, (req.subdir or "").strip())
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"status": "ok", "project_name": safe_project_name, "subdir": (req.subdir or "").strip()}


@app.post("/api/projects")
def create_project(req: ProjectCreateRequest):
    """Create a project directory immediately; initialize conda in the background."""
    try:
        workspace = ensure_project_directory(req.name)
        notes_path = _append_project_notes(
            workspace.project_dir,
            environment_requirements=req.environment_requirements,
            compute_requirements=req.compute_requirements,
        )
        conda_status = _start_conda_init(workspace.project_name)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"创建作业目录失败: {e}"}, status_code=500)

    return {
        "status": "ok",
        "project_name": workspace.project_name,
        "project_dir": str(workspace.project_dir),
        "conda_env_dir": str(workspace.conda_env_dir),
        "conda_created": False,
        "conda_status": conda_status.get("status"),
        "conda_message": conda_status.get("message"),
        "notes_path": str(notes_path),
    }


@app.get("/api/projects/conda-status")
def project_conda_status(project_name: str, start: bool = True):
    """Return per-project conda initialization status; optionally start it."""
    try:
        if start:
            status = _start_conda_init(project_name)
        else:
            status = _conda_status_for(project_name)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("读取项目 Conda 环境状态失败")
        return JSONResponse({"error": f"读取项目 Conda 环境状态失败: {e}"}, status_code=500)
    return {
        "status": "ok",
        "conda_status": status.get("status"),
        "project_name": status.get("project_name"),
        "project_dir": status.get("project_dir"),
        "conda_env_dir": status.get("conda_env_dir"),
        "message": status.get("message"),
        "error": status.get("error"),
        "started_at": status.get("started_at"),
        "finished_at": status.get("finished_at"),
        "created": status.get("created"),
    }


@app.post("/api/projects/subdirs")
def create_project_subdir(req: SubdirCreateRequest):
    """在项目下新建一个数据集小文件夹；名称为空时自动取名 数据集N。"""
    try:
        safe_project_name, project_dir, _ = project_workspace(req.project_name)
        subdirs = _project_subdirs(project_dir)
        if (req.name or "").strip():
            name = _validate_subdir_name(req.name)
        else:
            # 自动取名：数据集N，N 递增直到不重名
            index = len(subdirs) + 1
            while f"数据集{index}" in subdirs or (project_dir / f"数据集{index}").exists():
                index += 1
            name = f"数据集{index}"
        target = project_dir / name
        if target.exists():
            raise FileTransferError(f"小文件夹已存在: {name}")
        target.mkdir(parents=True)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("新建小文件夹失败")
        return JSONResponse({"error": f"新建小文件夹失败: {e}"}, status_code=500)
    return {
        "status": "ok",
        "project_name": safe_project_name,
        "subdir": name,
        "subdirs": _project_subdirs(project_dir),
    }


@app.post("/api/projects/subdirs/rename")
def rename_project_subdir(req: SubdirRenameRequest):
    """重命名项目内的数据集小文件夹（连同其会话记录）。"""
    try:
        safe_project_name, project_dir, _ = project_workspace(req.project_name)
        old_name = _validate_subdir_name(req.subdir)
        new_name = _validate_subdir_name(req.new_name)
        src = project_dir / old_name
        dst = project_dir / new_name
        if not src.is_dir():
            raise FileTransferError(f"小文件夹不存在: {old_name}")
        if dst.exists():
            raise FileTransferError(f"目标名称已存在: {new_name}")
        src.rename(dst)
        # 会话记录文件一并改名，保持会话与文件夹同步
        old_session = _subdir_session_path(project_dir, old_name)
        if old_session.exists():
            old_session.rename(_subdir_session_path(project_dir, new_name))
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("重命名小文件夹失败")
        return JSONResponse({"error": f"重命名小文件夹失败: {e}"}, status_code=500)
    return {
        "status": "ok",
        "project_name": safe_project_name,
        "subdir": new_name,
        "subdirs": _project_subdirs(project_dir),
    }


@app.post("/api/projects/subdirs/delete")
def delete_project_subdir(req: SubdirDeleteRequest):
    """递归删除项目内的数据集小文件夹及其会话记录（不可恢复）。"""
    try:
        safe_project_name, project_dir, _ = project_workspace(req.project_name)
        name = _validate_subdir_name(req.subdir)
        target = project_dir / name
        if not target.is_dir():
            raise FileTransferError(f"小文件夹不存在: {name}")
        shutil.rmtree(target)
        _subdir_session_path(project_dir, name).unlink(missing_ok=True)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("删除小文件夹失败")
        return JSONResponse({"error": f"删除小文件夹失败: {e}"}, status_code=500)
    return {
        "status": "ok",
        "project_name": safe_project_name,
        "subdir": name,
        "subdirs": _project_subdirs(project_dir),
    }


def _job_templates_dir() -> Path:
    """作业脚本模板的固定存储目录（项目之外、跨项目共享）：~/.slurm-agent/templates/。"""
    templates_dir = Path.home() / ".slurm-agent" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return templates_dir


@app.get("/api/job-templates")
def list_job_templates():
    """列出已保存的作业脚本模板（内容随列表一并返回，模板即完整 .sh 脚本）。"""
    try:
        templates = []
        for path in _job_templates_dir().glob("*.sh"):
            stat = path.stat()
            templates.append({
                "name": path.stem,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "content": path.read_text(encoding="utf-8"),
            })
        templates.sort(key=lambda t: t["mtime"], reverse=True)
    except Exception as e:
        logger.exception("读取作业模板列表失败")
        return JSONResponse({"error": f"读取作业模板列表失败: {e}"}, status_code=500)
    return {"status": "ok", "templates": templates}


@app.post("/api/job-templates")
def save_job_template(req: JobTemplateSaveRequest):
    """保存作业模板；新客户端提交结构化草稿，由服务端生成完整脚本。"""
    try:
        name = _validate_subdir_name(req.name, "模板")
        content = req.content or ""
        if req.project_name.strip():
            workspace = ensure_project_workspace(req.project_name)
            subdir = (req.subdir or "").strip().strip("/")
            run_dir = _resolve_run_dir(workspace.project_dir, subdir)
            job_name = re.sub(
                r"[^A-Za-z0-9_.-]", "", req.job_name or name
            ).strip(".-")
            if not job_name:
                raise FileTransferError(
                    "作业名只能包含字母、数字、点、下划线和短横线"
                )
            if len(req.command) > 100_000:
                raise FileTransferError("作业命令过长（最多 100000 字符）")
            draft = {
                "command": (req.command or "").replace("\r\n", "\n").replace("\r", "\n").strip(),
                "job_name": job_name,
                "partition": req.partition,
                "account": req.account,
                "qos": req.qos,
                "nodes": req.nodes,
                "cpus_per_task": req.cpus_per_task,
                "gpus_per_node": req.gpus_per_node,
                "memory_mb": req.memory_mb,
                "time_limit": req.time_limit,
            }
            if not all((draft["partition"], draft["account"], draft["qos"])):
                raise FileTransferError("分区、计费账户和 QoS 均不能为空")
            content = _build_controlled_job_script(draft, workspace, run_dir)
        if not content.strip():
            raise FileTransferError("脚本内容不能为空")
        if len(content) > 512 * 1024:
            raise FileTransferError("脚本内容过大（最多 512KB）")
        path = _job_templates_dir() / f"{name}.sh"
        overwritten = path.exists()
        path.write_text(content, encoding="utf-8")
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("保存作业模板失败")
        return JSONResponse({"error": f"保存作业模板失败: {e}"}, status_code=500)
    return {
        "status": "ok",
        "name": name,
        "overwritten": overwritten,
        "path": str(path),
    }


@app.post("/api/job-templates/delete")
def delete_job_template(req: JobTemplateDeleteRequest):
    """删除一个作业脚本模板。"""
    try:
        name = _validate_subdir_name(req.name, "模板")
        path = _job_templates_dir() / f"{name}.sh"
        if not path.is_file():
            raise FileTransferError(f"模板不存在: {name}")
        path.unlink()
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("删除作业模板失败")
        return JSONResponse({"error": f"删除作业模板失败: {e}"}, status_code=500)
    return {"status": "ok", "name": name}


@app.post("/api/projects/report")
def project_report(req: ProjectReportRequest):
    """Generate a dependency/environment plan before installing dependencies."""
    try:
        conda_status = _conda_status_for(req.name)
        if conda_status.get("status") != "ready":
            if conda_status.get("status") in {"missing", "failed"}:
                conda_status = _start_conda_init(req.name)
            return JSONResponse({
                "error": conda_status.get("message") or "项目 Conda 环境尚未就绪",
                "conda_status": conda_status.get("status"),
                "conda_env_dir": conda_status.get("conda_env_dir"),
            }, status_code=409)
        workspace = ensure_project_workspace(req.name)
        notes_path = _append_project_notes(
            workspace.project_dir,
            extra_notes=req.extra_notes,
        )
        notes_text = notes_path.read_text(encoding="utf-8", errors="ignore") if notes_path.exists() else req.extra_notes
        scanned_items = merge_dependency_items(
            scan_project_dependencies(workspace.project_dir)
            + scan_user_dependency_notes(notes_text)
        )
        # 先对静态扫描结果做版本感知预检，把真实可用版本/构建喂给 LLM，
        # 避免 LLM 凭旧知识（或其它集群的 module 版本号）指定不存在的版本
        scanned_items = precheck_dependencies(scanned_items, workspace.conda_env_dir)
        llm = LLMProvider()
        ai_items: list[DependencyItem] = []
        try:
            response = llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "你是谨慎的依赖识别助手，只返回严格 JSON 数组。",
                    },
                    {"role": "user", "content": _build_ai_dependency_json_prompt(workspace, scanned_items, req.extra_notes, notes_text)},
                ],
                temperature=0.1,
                max_tokens=1400,
            )
            ai_items = parse_ai_dependency_items(response.choices[0].message.content or "")
        except Exception:
            logger.exception("AI 依赖补充失败，继续使用静态扫描结果")

        # AI 新增的包再单独预检（扫描项已检过，search 有缓存不会重复查）
        scanned_keys = {(item.manager, item.name.lower()) for item in scanned_items}
        new_ai_items = [
            item for item in ai_items
            if (item.manager, item.name.lower()) not in scanned_keys
        ]
        new_ai_items = precheck_dependencies(new_ai_items, workspace.conda_env_dir)
        dependency_items = merge_dependency_items(scanned_items + new_ai_items)
        report = items_to_markdown(
            dependency_items,
            "下面是根据依赖文件、脚本、源码 import 和用户补充需求得到的安装清单。请在弹窗中确认勾选项后再安装。",
        )
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("生成依赖安装方案失败")
        return JSONResponse({"error": f"生成依赖安装方案失败: {e}"}, status_code=500)

    return {
        "status": "ok",
        "project_name": workspace.project_name,
        "project_dir": str(workspace.project_dir),
        "conda_env_dir": str(workspace.conda_env_dir),
        "conda_created": workspace.conda_created,
        "notes_path": str(notes_path),
        "report": report,
        "dependency_items": serialize_items(dependency_items),
    }


INSTALL_COMMAND_TIMEOUT = int(os.environ.get("SLURM_INSTALL_TIMEOUT", "1800"))

# ---------------------------------------------------------------------------
# 安装引擎：预取下载进度 + SSE 事件流
# ---------------------------------------------------------------------------


def _conda_pkgs_dir(conda_exe: str) -> Optional[Path]:
    """解析 conda 包缓存目录（取第一个 pkgs_dirs），预取下载会写到那里。"""
    try:
        result = subprocess.run(
            [conda_exe, "info", "--json"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    data = _extract_json_object((result.stdout or "") + "\n" + (result.stderr or ""))
    if isinstance(data, dict):
        dirs = data.get("pkgs_dirs") or []
        if dirs:
            try:
                pkgs = Path(str(dirs[0])).expanduser()
                pkgs.mkdir(parents=True, exist_ok=True)
                return pkgs
            except OSError:
                return None
    return None


def _prefetch_conda_packages(conda_exe: str, install_argv: list[str], on_progress) -> Optional[str]:
    """
    先 dry-run 拿事务计划，再自己流式下载 FETCH 列表到 conda pkgs 缓存目录。

    背景：conda 25.x 的 --json 只在结束时输出一个 JSON 对象，没有增量进度；
    而 conda 看到包已在 pkgs 缓存里就会跳过下载。因此用字节级自下载实现真实百分比，
    之后再执行真正的 conda install（全部命中缓存，秒级完成）。

    返回 None 表示成功或无需预取；返回错误字符串表示失败（调用方回退直接安装）。
    """
    dry_argv = [*install_argv, "--dry-run", "--json"]
    try:
        proc = subprocess.run(dry_argv, capture_output=True, text=True, timeout=300)
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"dry-run 执行失败： {e}"
    if proc.returncode != 0:
        return "dry-run 返回非零"
    data = _extract_json_object((proc.stdout or "") + "\n" + (proc.stderr or ""))
    if not isinstance(data, dict) or not isinstance(data.get("actions"), dict):
        return "dry-run 未返回事务计划"
    fetch_list = [p for p in (data["actions"].get("FETCH") or []) if isinstance(p, dict)]
    if not fetch_list:
        return None

    pkgs_dir = _conda_pkgs_dir(conda_exe)
    if pkgs_dir is None:
        return "无法解析 pkgs 缓存目录"
    pending = [
        p for p in fetch_list
        if p.get("fn") and p.get("url") and not (pkgs_dir / str(p["fn"])).exists()
    ]
    if not pending:
        return None

    total_bytes = sum(int(p.get("size") or 0) for p in pending)
    done_bytes = 0
    for pkg in pending:
        fn = str(pkg["fn"])
        target = pkgs_dir / fn
        tmp = pkgs_dir / f"{fn}.agent-{os.getpid()}.part"
        try:
            with requests.get(str(pkg["url"]), stream=True, timeout=(15, 120)) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as out:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        out.write(chunk)
                        done_bytes += len(chunk)
                        on_progress(done_bytes, total_bytes, fn)
            os.replace(tmp, target)
        except Exception as e:  # 预取失败不阻断安装，回退给 conda 自己下载
            try:
                tmp.unlink()
            except OSError:
                pass
            return f"预取 {fn} 失败： {e}"
    return None


def _emit_pip_stage(line: str, emit_stage) -> None:
    """从 pip 输出中解析粗粒度阶段，推送进度文本。"""
    stripped = line.strip()
    lowered = stripped.lower()
    if lowered.startswith("collecting "):
        emit_stage(f"解析 {stripped.split()[1]}", 30)
    elif lowered.startswith("downloading "):
        emit_stage(f"下载 {stripped.split()[1]}", 50)
    elif lowered.startswith("installing collected packages"):
        emit_stage("安装包到环境", 70)
    elif lowered.startswith("successfully installed"):
        emit_stage("安装完成", 95)


def _run_install_command(
    command: str,
    argv: list[str],
    cwd: Path,
    emit_stage,
) -> tuple[int, str]:
    """
    执行单条安装命令。

    conda 命令：先预取（带字节级下载进度），再正式安装（命中缓存）。
    返回 (returncode, output)。
    """
    is_conda = "install" in [part.lower() for part in argv[:3]] and not argv[0].endswith("python")
    if is_conda:
        emit_stage("求解依赖中（生成事务计划）", 5)

        def _on_download(done: int, total: int, fn: str) -> None:
            if total > 0:
                emit_stage(f"下载 {fn}（{_fmt_bytes(done)}/{_fmt_bytes(total)}）", 5 + 85.0 * done / total)
            else:
                emit_stage(f"下载 {fn}（{_fmt_bytes(done)}）", None)

        prefetch_error = _prefetch_conda_packages(argv[0], argv, _on_download)
        if prefetch_error:
            logger.info("conda 预取跳过，回退直接安装：%s", prefetch_error)
        emit_stage("解包并安装到项目环境", 90)

    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    output_lines: list[str] = []

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            output_lines.append(line)
            if not is_conda:
                try:
                    _emit_pip_stage(line, emit_stage)
                except Exception:
                    pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=INSTALL_COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        output_lines.append("\n[安装命令超时，已被终止]")
    reader.join(timeout=10)
    return proc.returncode, "".join(output_lines)


def _fmt_bytes(value: int) -> str:
    value = max(0, int(value))
    if value >= 1024 * 1024 * 1024:
        return f"{value / 1024 / 1024 / 1024:.1f} GB"
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value} B"


def _package_names_from_command(command: str) -> list[str]:
    """从展示用的安装命令里提取包名（去掉版本与构建、去掉选项及选项值）。"""
    try:
        parts = shlex.split(command)
    except ValueError:
        return []
    names: list[str] = []
    skip_next = False
    for part in parts[2:]:
        if skip_next:
            skip_next = False
            continue
        if part in {"-c", "--channel", "-p", "--prefix", "-n", "--name", "-r", "--requirement"}:
            skip_next = True
            continue
        if part.startswith("-"):
            continue
        name = re.split(r"[=<>=!~\s]", part, 1)[0].strip("._-")
        if name and name.lower() not in {"install"} and name not in names:
            names.append(name)
    return names


def _llm_fix_install_commands(
    failed_command: str,
    output: str,
    selected_items: list[dict],
) -> Optional[tuple[list[str], str]]:
    """
    安装失败后的自动修复：查询软件源真实版本 → LLM 重新选版 → 返回修正命令。

    返回 (commands, reason)；失败返回 None。命令会经过 _extract_install_commands
    白名单过滤与 _normalize_install_command 归一化，保证安全。
    """
    names = _package_names_from_command(failed_command)
    if not names:
        return None
    search_lines: list[str] = []
    for name in names[:8]:
        search = search_package_versions(name)
        if search["ok"]:
            search_lines.append(
                f"### {name}\n可用版本（旧→新）：{', '.join(search['versions'][-10:])}\n"
                f"最近构建：{', '.join(search['builds'][-12:])}"
            )
        else:
            search_lines.append(f"### {name}\n查询失败：{search['error']}")

    selected_text = "\n".join(
        f"- {item.get('name')} {item.get('version') or '(未指定版本)'} ({item.get('manager', 'conda')})"
        for item in selected_items[:40] if isinstance(item, dict)
    ) or "（无）"

    prompt = f"""conda/pip 安装命令执行失败了。请根据包管理器的真实查询结果修正安装命令。

失败的命令：
{failed_command}

失败输出（截断）：
{_trim_text(output, 3000)}

<包管理查询结果（真实可用版本与构建）>
{chr(10).join(search_lines)}
</包管理查询结果>

<集群硬件上下文>
{_hardware_context_text()}
</集群硬件上下文>

<用户原本勾选的依赖>
{selected_text}
</用户原本勾选的依赖>

要求：
1. 只返回严格 JSON：{{"commands": ["conda install ...", ...], "reason": "一句话说明改了什么"}}
2. commands 里只能有 conda install / pip install 命令，每条一行。
3. 版本号必须来自上面的查询结果；需要 GPU 的包按硬件上下文选择构建（conda 三段式如 gromacs=2026.3=nompi_cuda）。
4. 查询结果里没有合适的包/版本时，commands 返回空数组，并在 reason 里说明原因。"""

    try:
        llm = LLMProvider()
        response = llm.chat(
            messages=[
                {"role": "system", "content": "你是依赖安装修复助手，只返回严格 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=800,
        )
    except Exception:
        logger.exception("安装修复 LLM 调用失败")
        return None

    raw = response.choices[0].message.content or ""
    match = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.S)
    if match:
        raw = match.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    commands = [str(c) for c in (data.get("commands") or []) if isinstance(c, str) and c.strip()]
    reason = str(data.get("reason") or "").strip()[:500]
    # 白名单过滤，只保留可识别的安装命令
    commands = _extract_install_commands("\n".join(commands))
    if not commands:
        return None
    return commands, reason


@app.post("/api/projects/install-deps")
def install_project_dependencies(req: ProjectInstallRequest):
    """
    执行用户确认过的依赖安装，SSE 流式返回进度百分比与结果。

    进度机制：conda 命令先 dry-run 拿事务计划，再自下载包到 pkgs 缓存
    （字节级真实百分比），最后正式安装（命中缓存）；pip 用阶段标记粗粒度推进。
    安装失败时自动携带真实包查询结果满 LLM 重新选版并重试一次。
    """
    try:
        # 先确保工作区就绪（可能触发 conda create，内部自会加项目锁）；
        # 之后再拿锁执行安装，避免嵌套拿锁死锁
        conda_status = _conda_status_for(req.name)
        if conda_status.get("status") != "ready":
            if conda_status.get("status") in {"missing", "failed"}:
                conda_status = _start_conda_init(req.name)
            return JSONResponse({
                "error": conda_status.get("message") or "项目 Conda 环境尚未就绪",
                "conda_status": conda_status.get("status"),
                "conda_env_dir": conda_status.get("conda_env_dir"),
            }, status_code=409)
        workspace = ensure_project_workspace(req.name)
        selected_items = req.selected_items or []
        commands = _commands_from_selected_items(selected_items) or _extract_install_commands(req.plan)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not commands:
        return JSONResponse(
            {"error": "未选择可安装依赖，也未在方案中找到可执行的 conda/mamba/pip install 命令"},
            status_code=400,
        )

    def event_stream():
        events: queue.Queue = queue.Queue()
        sentinel = object()

        class _Progress:
            """线程安全地限流推送进度事件（阶段或百分比变化时才推）。"""

            def __init__(self, command_index: int, command_total: int):
                self.base = command_index * 100.0 / command_total
                self.span = 100.0 / command_total
                self._last_percent: Optional[float] = None
                self._last_stage = ""
                self._last_ts = 0.0

            def stage(self, text: str, percent: Optional[float] = None) -> None:
                now = datetime.now().timestamp()
                overall = None
                if percent is not None:
                    overall = self.base + self.span * min(100.0, max(0.0, percent)) / 100.0
                stage_changed = text != self._last_stage
                percent_changed = (
                    overall is not None
                    and (self._last_percent is None or abs(overall - self._last_percent) >= 1.0)
                )
                if not (stage_changed or percent_changed):
                    return
                # 阶段变化立即推送；纯百分比刷新限流到每 0.5 秒一次
                if not stage_changed and now - self._last_ts < 0.5:
                    return
                self._last_percent = overall
                self._last_stage = text
                self._last_ts = now
                events.put({
                    "type": "progress",
                    "percent": round(overall, 1) if overall is not None else None,
                    "stage": text,
                })

        def worker() -> None:
            results: list[dict] = []
            auto_fix_info: Optional[dict] = None
            try:
                with project_lock(workspace.project_dir):
                    queue_commands = list(commands)
                    index = 0
                    fixed_for_current = False
                    while index < len(queue_commands):
                        command = queue_commands[index]
                        events.put({"type": "command_start", "index": index, "command": command,
                                    "total": len(queue_commands)})
                        progress = _Progress(index, len(queue_commands))
                        try:
                            argv = _normalize_install_command(command, workspace.conda_env_dir)
                        except FileTransferError as e:
                            results.append({"command": command, "executed": "", "returncode": 1,
                                            "output": str(e)})
                            events.put({"type": "command_done", "index": index, "returncode": 1,
                                        "output": str(e)})
                            break
                        returncode, output = _run_install_command(
                            command, argv, workspace.project_dir, progress.stage,
                        )
                        results.append({
                            "command": command,
                            "executed": " ".join(shlex.quote(part) for part in argv),
                            "returncode": returncode,
                            "output": _trim_text(output.strip(), 4000),
                        })
                        events.put({"type": "command_done", "index": index, "returncode": returncode,
                                    "output": _trim_text(output.strip(), 800)})
                        if returncode == 0:
                            index += 1
                            fixed_for_current = False
                            continue

                        # 失败：先尝试 LLM 携真实版本自动修复，重试一次
                        if not fixed_for_current:
                            events.put({"type": "auto_fix", "status": "analyzing",
                                        "failed_command": command})
                            fix = _llm_fix_install_commands(command, output, selected_items)
                            if fix:
                                fixed_commands, reason = fix
                                auto_fix_info = {
                                    "applied": True,
                                    "original_command": command,
                                    "commands": fixed_commands,
                                    "reason": reason,
                                }
                                # 原失败结果标记为已被自动修复取代：重试成功后
                                # 最终成败只看修正命令，不因历史失败误报
                                if results:
                                    results[-1]["superseded"] = True
                                events.put({"type": "auto_fix", "status": "retry",
                                            "reason": reason, "commands": fixed_commands})
                                # 用修正命令替换失败命令，后续原命令继续执行
                                queue_commands = (
                                    queue_commands[:index] + fixed_commands + queue_commands[index + 1:]
                                )
                                fixed_for_current = True
                                continue
                            events.put({"type": "auto_fix", "status": "failed",
                                        "reason": "未能生成修正命令"})
                        break

                    effective_results = [r for r in results if not r.get("superseded")]
                    if any(r.get("returncode") for r in effective_results):
                        events.put({"type": "error", "payload": {
                            "error": "依赖安装失败：" + next(
                                (r["command"] for r in effective_results if r.get("returncode")), ""
                            ),
                            "status": "failed",
                            "project_name": workspace.project_name,
                            "conda_env_dir": str(workspace.conda_env_dir),
                            "results": results,
                            "auto_fix": auto_fix_info,
                        }})
                        return

                    # 验证也在锁内完成，避免刚装完就被并发操作覆盖
                    validation_results = _validate_installed_items(selected_items, workspace.conda_env_dir)
                events.put({"type": "done", "payload": {
                    "status": "ok",
                    "project_name": workspace.project_name,
                    "project_dir": str(workspace.project_dir),
                    "conda_env_dir": str(workspace.conda_env_dir),
                    "results": results,
                    "validation_results": validation_results,
                    "auto_fix": auto_fix_info,
                }})
            except Exception as e:
                logger.exception("安装项目依赖失败")
                events.put({"type": "error", "payload": {
                    "error": f"安装项目依赖失败: {e}",
                    "status": "failed",
                    "project_name": workspace.project_name,
                    "conda_env_dir": str(workspace.conda_env_dir),
                    "results": results,
                }})
            finally:
                events.put(sentinel)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = events.get()
            if item is sentinel:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/projects/job-skeleton")
def project_job_skeleton(project_name: str, subdir: str = ""):
    """
    下发作业脚本“锁定区”的权威值：真实运行目录（项目根或某个小文件夹）、
    conda 环境路径、conda.sh 绝对路径与固定激活前导。前端只展示不可改。
    """
    try:
        workspace = _light_workspace(project_name)
        run_dir = _resolve_run_dir(workspace.project_dir, subdir)
        conda_sh = _conda_sh_path()
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("获取作业脚本骨架失败")
        return JSONResponse({"error": f"获取作业脚本骨架失败: {e}"}, status_code=500)

    prelude = _build_job_prelude(workspace, run_dir)
    return {
        "status": "ok",
        "project_name": workspace.project_name,
        "project_dir": str(workspace.project_dir),
        "run_dir": str(run_dir),
        "subdir": (subdir or "").strip().strip("/"),
        "conda_env_dir": str(workspace.conda_env_dir),
        "conda_sh": conda_sh,
        "prelude": prelude,
    }


@app.post("/api/projects/job-body")
def project_job_body(req: JobBodyRequest):
    """一次 LLM 调用生成作业命令正文；#SBATCH 头与目录/环境激活由锁定区负责。"""
    try:
        workspace = _light_workspace(req.name)
        run_dir = _resolve_run_dir(workspace.project_dir, (req.subdir or "").strip())
        prompt = _build_job_body_prompt(workspace, req.form, run_dir)
        llm = LLMProvider()
        response = llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": "你是 Slurm 作业命令生成器，只输出 bash 命令正文，不要任何解释。",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1200,
        )
        body = _extract_bash_body(response.choices[0].message.content or "")
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("生成作业命令正文失败")
        return JSONResponse({"error": f"生成作业命令正文失败: {e}"}, status_code=500)

    if not body:
        return JSONResponse({"error": "模型未返回有效命令，请在正文区手动填写"}, status_code=500)
    path_errors = _command_path_errors(body, run_dir)
    return {
        "status": "ok",
        "project_name": workspace.project_name,
        "run_dir": str(run_dir),
        "body": body,
        "path_errors": path_errors,
    }


@app.post("/api/jobs/submit")
def submit_project_job(req: JobSubmitRequest):
    """Web 提交入口：只接收结构化草稿，复用 Agent 的同一受控后端。"""
    try:
        return _submit_controlled_job({
            "source": "web",
            "project_name": req.project_name,
            "subdir": req.subdir,
            "command": req.command,
            "job_name": req.job_name,
            "partition": req.partition,
            "account": req.account,
            "qos": req.qos,
            "nodes": req.nodes,
            "cpus_per_task": req.cpus_per_task,
            "gpus_per_node": req.gpus_per_node,
            "memory_mb": req.memory_mb,
            "time_limit": req.time_limit,
        })
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("提交作业失败")
        return JSONResponse({"error": f"提交作业失败: {e}"}, status_code=500)


# ---------------------------------------------------------------------------
# 作业状态心跳：跟踪本服务提交的作业，发现 COMPLETED/FAILED 时通知前端；
# 完成可打包下载输出目录，失败由 LLM 阅读日志生成简报
# ---------------------------------------------------------------------------
JOB_WATCH_FILE = Path.home() / ".slurm-agent" / "job-watch.json"
JOB_FAILED_STATES = {"FAILED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY"}
_job_watch_lock = threading.Lock()


def _load_job_watch() -> list[dict]:
    try:
        raw = json.loads(JOB_WATCH_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return raw
    except (OSError, ValueError):
        pass
    return []


def _save_job_watch(records: list[dict]) -> None:
    try:
        JOB_WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        JOB_WATCH_FILE.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        logger.exception("保存作业监控记录失败")


def _register_watched_job(job_id, job_name: str, project_name: str, subdir: str, logs_dir: Path) -> None:
    """提交成功后登记作业，供心跳查询与结果定位。"""
    if job_id is None:
        return
    with _job_watch_lock:
        records = _load_job_watch()
        records.append({
            "job_id": str(job_id),
            "job_name": job_name,
            "project_name": project_name,
            "subdir": subdir,
            "logs_dir": str(logs_dir),
            "state": "SUBMITTED",
            "final_state": "",
            "report_ready": False,
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
        })
        # 只保留最近 200 条，避免无限增长
        _save_job_watch(records[-200:])


def _job_state_text(raw) -> str:
    """统一 REST 返回的状态字段：['FAILED'] / 'FAILED' / {'current': [...]} → 大写字符串。"""
    if isinstance(raw, list) and raw:
        return "+".join(str(s).upper() for s in raw)
    if isinstance(raw, str) and raw:
        return raw.upper()
    return ""


def _watched_job_states(job_ids: list[str]) -> dict[str, str]:
    """REST 查询已登记作业的当前状态，返回 {job_id: STATE}。

    本平台 sacct 对普通用户零返回（实测无输出、无报错），心跳一律走 REST：
    - slurmctld /jobs 实时快照：覆盖运行中与刚结束（尚未被控制器清除）的作业；
    - slurmdbd /jobs 历史（job_id 过滤）：补上已被清除出控制器的终态作业。
    单个查询失败只记日志，不影响其他作业，下一轮心跳重试。
    """
    client = SlurmClient()
    wanted = {str(j) for j in job_ids}
    states: dict[str, str] = {}
    # 1) slurmctld 实时列表
    try:
        for job in client.list_jobs().get("jobs") or []:
            jid = str(job.get("job_id") or "")
            if jid not in wanted:
                continue
            state = _job_state_text(job.get("job_state"))
            if state:
                states[jid] = state
    except Exception as e:
        logger.warning("list_jobs 查询失败: %s", e)
    # 2) 已清除出控制器的，逐个查 slurmdbd 历史
    for jid in wanted - set(states):
        try:
            for job in client.get_jobs_history(params={"job_id": jid}).get("jobs") or []:
                if str(job.get("job_id")) != jid:
                    continue
                state = job.get("state")
                if isinstance(state, dict):
                    state = _job_state_text(state.get("current"))
                else:
                    state = _job_state_text(state)
                if state:
                    states[jid] = state
                break
        except Exception as e:
            logger.warning("slurmdbd 历史查询失败 job=%s: %s", jid, e)
    return states


def _watched_record(job_id: str) -> Optional[dict]:
    for record in _load_job_watch():
        if record.get("job_id") == str(job_id):
            return record
    return None


def _update_watched_record(job_id: str, **fields) -> Optional[dict]:
    with _job_watch_lock:
        records = _load_job_watch()
        for record in records:
            if record.get("job_id") == str(job_id):
                record.update(fields)
                _save_job_watch(records)
                return record
    return None


@app.get("/api/jobs/watch")
def jobs_watch():
    """心跳：查询已登记作业状态，返回新发生的 COMPLETE/FAILED 事件（不重复报）。"""
    with _job_watch_lock:
        records = _load_job_watch()
    pending = [r for r in records if not r.get("final_state")]
    events: list[dict] = []
    if pending:
        try:
            states = _watched_job_states([r["job_id"] for r in pending])
        except Exception as e:  # REST 整体不可用时静默，下轮再试
            logger.warning("作业状态查询失败: %s", e)
            return {"status": "ok", "events": []}
        changed = False
        for record in pending:
            state = states.get(record["job_id"])
            if not state:
                continue
            record["state"] = state
            if state.startswith("CANCELLED"):
                record["final_state"] = state  # 主动取消：忽略，不产生事件
                changed = True
            elif state == "COMPLETED" or state in JOB_FAILED_STATES:
                record["final_state"] = state
                changed = True
                events.append({
                    "job_id": record["job_id"],
                    "job_name": record.get("job_name", ""),
                    "state": state,
                    "project_name": record.get("project_name", ""),
                    "subdir": record.get("subdir", ""),
                    "logs_dir": record.get("logs_dir", ""),
                })
        if changed:
            with _job_watch_lock:
                _save_job_watch(records)
    return {"status": "ok", "events": events}


@app.get("/api/jobs/{job_id}/results")
def job_results(job_id: str):
    """打包下载作业输出目录（logs/，含 .out/.err/脚本及产出）。"""
    record = _watched_record(job_id)
    if not record:
        return JSONResponse({"error": "未找到该作业的记录"}, status_code=404)
    logs_dir = Path(record["logs_dir"])
    if not logs_dir.is_dir():
        return JSONResponse({"error": "输出目录不存在"}, status_code=404)

    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(logs_dir.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(logs_dir.parent))
    buffer.seek(0)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "", record.get("job_name", "job")) or "job"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="results-{safe_name}-{job_id}.zip"'
        },
    )


def _run_failure_analysis(record: dict) -> None:
    """后台线程：读 .err/.out/脚本，调 LLM 生成失败简报写到输出目录。"""
    job_id = record["job_id"]
    try:
        logs_dir = Path(record["logs_dir"])
        err_files = sorted(logs_dir.glob("*.err"), key=lambda p: p.stat().st_mtime, reverse=True)
        out_files = sorted(logs_dir.glob("*.out"), key=lambda p: p.stat().st_mtime, reverse=True)
        script_files = sorted(logs_dir.glob("*.sh"), key=lambda p: p.stat().st_mtime, reverse=True)

        err_text = err_files[0].read_text(encoding="utf-8", errors="ignore")[-8000:] if err_files else "（未找到 .err 文件）"
        out_text = out_files[0].read_text(encoding="utf-8", errors="ignore")[-2500:] if out_files else "（未找到 .out 文件）"
        script_text = script_files[0].read_text(encoding="utf-8", errors="ignore")[:4000] if script_files else "（未找到脚本）"

        llm = LLMProvider()
        response = llm.chat([
            {
                "role": "system",
                "content": (
                    "你是 HPC 集群的 Slurm 作业诊断专家。请根据作业的 stderr/stdout 与提交脚本，"
                    "用中文写一份简洁的 markdown 失败分析简报，结构：\n"
                    "# 作业失败分析：<作业名>\n"
                    "## 失败状态\n（一句话）\n"
                    "## 关键错误\n（引用最重要的原始错误行，代码块）\n"
                    "## 原因分析\n（2-4 条要点）\n"
                    "## 修复建议\n（可操作的具体步骤）\n"
                    "只输出简报正文。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"作业 ID: {job_id}\n作业名: {record.get('job_name', '')}\n"
                    f"项目: {record.get('project_name', '')}\n最终状态: {record.get('final_state', '')}\n\n"
                    "## stderr（尾部）\n```\n" + err_text + "\n```\n\n"
                    "## stdout（尾部）\n```\n" + out_text + "\n```\n\n"
                    "## 提交脚本\n```bash\n" + script_text + "\n```"
                ),
            },
        ])
        report = (response.choices[0].message.content or "").strip()
        if not report:
            raise RuntimeError("LLM 返回空内容")
        report_path = logs_dir / f"failure-report-{job_id}.md"
        report_path.write_text(report + "\n", encoding="utf-8")
        _update_watched_record(job_id, report_ready=True, report_error="")
        logger.info("作业 %s 失败简报已生成: %s", job_id, report_path)
    except Exception as e:
        logger.exception("作业 %s 失败分析失败", job_id)
        _update_watched_record(job_id, report_ready=False, report_error=str(e)[:300])


@app.post("/api/jobs/{job_id}/analyze-failure")
def job_analyze_failure(job_id: str):
    """触发后台 LLM 失败分析（不阻塞），前端轮询 /report 获取结果。"""
    record = _watched_record(job_id)
    if not record:
        return JSONResponse({"error": "未找到该作业的记录"}, status_code=404)
    if record.get("report_ready"):
        return {"status": "ok", "ready": True}
    if record.get("analyzing"):
        return {"status": "ok", "ready": False}
    _update_watched_record(job_id, analyzing=True)
    threading.Thread(target=_run_failure_analysis, args=(record,), daemon=True).start()
    return {"status": "ok", "ready": False}


def _fs_tree(root: Path, base: Optional[Path] = None) -> list[dict]:
    """通用目录树（docs 树同构）：目录优先、文件按名排序，供报告阅读器左侧展示。"""
    base = base or root
    nodes: list[dict] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: (0 if p.is_dir() else 1, p.name))
    except OSError:
        return nodes
    for entry in entries:
        if entry.name.startswith("."):
            continue
        rel = entry.relative_to(base).as_posix()
        if entry.is_dir():
            children = _fs_tree(entry, base)
            if children:
                nodes.append({"name": entry.name, "title": entry.name, "type": "dir", "path": rel, "children": children})
        else:
            nodes.append({"name": entry.stem, "title": entry.name, "type": "file", "path": rel})
    return nodes


@app.get("/api/jobs/{job_id}/report")
def job_report(job_id: str):
    """返回失败分析简报内容与输出目录树（报告就绪前返回 pending）。"""
    record = _watched_record(job_id)
    if not record:
        return JSONResponse({"error": "未找到该作业的记录"}, status_code=404)
    if record.get("analyzing") and not record.get("report_ready"):
        return {"status": "pending"}
    logs_dir = Path(record["logs_dir"])
    report_path = logs_dir / f"failure-report-{job_id}.md"
    if not report_path.is_file():
        return {"status": "pending"}
    if record.get("report_ready") is False and record.get("report_error"):
        return JSONResponse({"error": f"分析失败: {record['report_error']}"}, status_code=500)
    return {
        "status": "ok",
        "job_id": job_id,
        "job_name": record.get("job_name", ""),
        "title": f"失败分析 · {record.get('job_name', job_id)} ({job_id})",
        "report_path": report_path.relative_to(logs_dir).as_posix(),
        "content": report_path.read_text(encoding="utf-8", errors="ignore"),
        "tree": _fs_tree(logs_dir),
    }


@app.post("/api/files/upload")
async def files_upload(
    project_name: str = Form(...),
    files: list[UploadFile] = File(...),
    subdir: str = Form(""),
):
    """
    Receive browser-selected files, stage them into a temp directory, and copy
    them straight into the project directory or one of its dataset subdirs
    (merge-overwrite semantics).

    The backend deliberately accepts file streams only; it does not take local
    filesystem paths from the browser.
    """
    if not files:
        return JSONResponse({"error": "请选择至少一个文件"}, status_code=400)

    max_bytes = get_max_upload_bytes()
    total_bytes = 0

    try:
        with tempfile.TemporaryDirectory(prefix="slurm-agent-upload-") as tmp:
            staging_dir = Path(tmp)
            file_count = 0

            for upload in files:
                relative_path = safe_relative_path(upload.filename or "uploaded-file")
                target = staging_dir / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)

                with target.open("wb") as out:
                    while True:
                        chunk = await upload.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > max_bytes:
                            raise FileTransferError(
                                f"上传总大小超过限制：{max_bytes} bytes"
                            )
                        out.write(chunk)
                file_count += 1

            # 拷贝落盘/建环境都是阻塞操作，丢到线程池执行，避免冻结事件循环
            result = await asyncio.to_thread(
                copy_files_to_project, staging_dir, file_count, project_name, (subdir or "").strip()
            )

    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        logger.exception("文件上传失败")
        return JSONResponse({"error": f"文件上传失败: {e}"}, status_code=500)
    finally:
        for upload in files:
            await upload.close()

    return {
        "status": "ok",
        "upload_id": result.upload_id,
        "project_name": result.project_name,
        "file_count": result.file_count,
        "total_bytes": result.total_bytes,
        "remote_project_dir": result.remote_project_dir,
        "conda_env_dir": result.conda_env_dir,
        "conda_created": result.conda_created,
    }


# ---------------------------------------------------------------------------
# 前端页面
# ---------------------------------------------------------------------------


@app.get("/")
async def index():
    """返回聊天界面 HTML 页面。"""
    from pathlib import Path
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h2>前端页面未找到，请创建 server/static/index.html</h2>")


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "agent_ready": agent is not None}


# =========================================================================
# __main__ 直接启动
# =========================================================================

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    uvicorn.run(app, host="0.0.0.0", port=8080)

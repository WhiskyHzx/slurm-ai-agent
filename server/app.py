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
import subprocess
import tempfile
import threading
import getpass
from datetime import datetime
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
    ensure_project_workspace,
    find_conda_executable,
    get_max_upload_bytes,
    get_remote_projects_base,
    package_and_upload,
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


def _history_path(project_name: str, create: bool = False) -> Path:
    safe_project_name, project_dir, _ = project_workspace(project_name)
    if create:
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / ".slurm-agent").mkdir(parents=True, exist_ok=True)
    return project_dir / ".slurm-agent" / "chat-history.json"


def _read_chat_history(project_name: str) -> list[dict]:
    path = _history_path(project_name)
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


def _write_chat_history(project_name: str, history: list[dict]) -> None:
    path = _history_path(project_name, create=True)
    path.write_text(
        json.dumps(history[-200:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _append_chat_history(project_name: str, role: str, content: str) -> None:
    if role not in {"user", "ai"} or not content.strip():
        return
    history = _read_chat_history(project_name)
    history.append({
        "role": role,
        "content": content,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    _write_chat_history(project_name, history)


def _agent_from_history(project_name: str) -> AgentLoop:
    ag = AgentLoop()
    for item in _read_chat_history(project_name)[-40:]:
        role = "assistant" if item["role"] == "ai" else "user"
        ag.messages.append({"role": role, "content": item["content"]})
    return ag


def get_agent(project_name: str = "") -> AgentLoop:
    """获取或懒初始化 AgentLoop 实例。"""
    global agent
    if project_name:
        safe_project_name, _, _ = project_workspace(project_name)
        if safe_project_name not in project_agents:
            project_agents[safe_project_name] = _agent_from_history(safe_project_name)
        return project_agents[safe_project_name]
    if agent is None:
        agent = AgentLoop()
    return agent


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    project_name: str = ""


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


class ProjectChatAppendRequest(BaseModel):
    project_name: str
    role: str
    content: str


class JobSubmitRequest(BaseModel):
    project_name: str
    script: str
    job_name: str
    partition: str
    nodes: int = 1
    time_limit: int = 240  # 分钟


class ModelSelectRequest(BaseModel):
    model: str


PROJECT_NOTES_FILENAME = "PROJECT_NOTES.txt"

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


def _summarize_job(job: dict) -> dict:
    return {
        "id": job.get("job_id") or job.get("jobid") or job.get("id") or "-",
        "name": job.get("name") or job.get("job_name") or "-",
        "user": job.get("user_name") or job.get("user") or "-",
        "partition": job.get("partition") or "-",
        "state": _state_text(job.get("job_state") or job.get("state")),
        "nodes": job.get("nodes") or job.get("node_count") or "-",
        "time_limit": job.get("time_limit") or job.get("time_limit_number") or "-",
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
2. 输出目录必须规范化：程序结果保存到项目目录下 runs/<作业名>-%j/。
3. Slurm 标准输出和标准错误必须使用 runs/<作业名>-%j.out 与 runs/<作业名>-%j.err。
4. 必要时在脚本开头 mkdir -p runs "$RUN_DIR"。
5. 每个项目的 conda 环境已准备在 <conda_env>，依赖安装命令优先使用该环境。
6. 如果依赖名称、入口命令、数据路径或算力需求不确定，必须列入“需要用户确认的问题”，不能擅自假设。
7. 如果包管理查询结果不足，请给出可复制的 conda/pip 查询命令。
8. “将要安装的程序环境列表”必须带版本或版本范围；版本号只能来自项目文件、用户输入或包管理查询结果中的真实版本，其它情况写“需确认”，不要编造。特别禁止把其它集群 module 系统里的版本号（如 gromacs/2019.4-gcc-9.2.0-openmpi 中的 2019.4）直接当作 conda/pip 可安装版本。
9. “安装命令”只能包含 conda/mamba/pip 安装命令，每行一条，不要写 rm、curl、wget、bash、sh、source、export 或其它 shell 操作。
10. 输出使用 Markdown，必须严格包含以下标题：
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

<项目目录摘要>
{_project_tree(workspace.project_dir)}
</项目目录摘要>

<可直接阅读的文本文件内容>
{_collect_readable_text_files(workspace.project_dir)}
</可直接阅读的文本文件内容>
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


def _build_job_body_prompt(workspace: ProjectWorkspace, form: dict) -> str:
    notes_path = workspace.project_dir / PROJECT_NOTES_FILENAME
    notes_text = notes_path.read_text(encoding="utf-8", errors="ignore") if notes_path.exists() else ""
    installed = installed_packages_snapshot(workspace.conda_env_dir)
    installed_text = ", ".join(sorted(installed)[:150]) or "（环境为空或尚未安装依赖）"
    form_lines = [f"- {key}: {value}" for key, value in (form or {}).items() if value not in (None, "")]
    form_text = "\n".join(form_lines) or "（用户未调整，均为默认值）"
    python_bin = workspace.conda_env_dir / "bin" / "python"
    context = f"""
你是 USTC 107 算力平台的 Slurm 作业命令生成器。请根据项目内容，生成 sbatch 脚本的“作业命令正文”。

<硬性规则>
1. 只输出作业命令正文（bash 命令与注释），不要输出 #!/bin/bash 和任何 #SBATCH 行——头部由系统生成。
2. 不要输出 cd、mkdir、conda 激活、source 等环境准备命令——系统已在正文之前固定处理：工作目录已切到项目目录，项目 Conda 环境已激活。
3. 主计算命令用 srun 开头（如 srun python -u train.py --epochs 10）。
4. 程序产生的输出文件保存到 runs/<作业名>-${{SLURM_JOB_ID}}/ 目录。
5. python 命令加 -u 实时输出；正文开头结尾用 echo 打印时间戳，便于排查。
6. 入口脚本、参数不确定时选最合理的默认，并在注释中标注“默认值，可修改”。
7. 只输出代码本身，不要 Markdown 代码块标记，不要解释文字。
</硬性规则>

<项目元信息>
- 项目目录：{workspace.project_dir}
- 项目 Conda 环境：{workspace.conda_env_dir}
- 环境 python：{python_bin}
</项目元信息>

<用户选择的作业参数>
{form_text}
</用户选择的作业参数>

<环境已安装的包（部分）>
{installed_text}
</环境已安装的包>

<用户需求记录>
{notes_text.strip() or "（无）"}
</用户需求记录>

<项目目录摘要>
{_project_tree(workspace.project_dir)}
</项目目录摘要>

<可直接阅读的文本文件内容>
{_collect_readable_text_files(workspace.project_dir)}
</可直接阅读的文本文件内容>
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
    if req.project_name.strip():
        try:
            project_name, _, _ = project_workspace(req.project_name)
        except FileTransferError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _append_chat_history(project_name, "user", user_message)

    async def event_stream():
        try:
            ag = get_agent(project_name)
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
                        _append_chat_history(project_name, "ai", error_text)
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

                        # 发送 tool_start 事件
                        yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name, 'args': arguments}, ensure_ascii=False)}\n\n"

                        try:
                            result_str = ag.executor.execute(tool_name, arguments)
                        except Exception as e:
                            result_str = f"工具执行出错: {e}"

                        # 发送 tool_end 事件
                        yield f"data: {json.dumps({'type': 'tool_end', 'tool': tool_name, 'result': result_str[:500]}, ensure_ascii=False)}\n\n"

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
                    _append_chat_history(project_name, "ai", final_reply)
                return

            # 超过最大轮数
            error_text = "超过最大工具调用轮数，请简化问题后重试。"
            if project_name:
                _append_chat_history(project_name, "ai", error_text)
            yield f"data: {json.dumps({'type': 'error', 'content': error_text}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.exception("处理请求异常")
            error_text = f"服务异常: {e}"
            if project_name:
                _append_chat_history(project_name, "ai", error_text)
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
    try:
        body = await req.json()
        project_name = str(body.get("project_name") or "").strip()
    except Exception:
        project_name = ""

    if project_name:
        safe_project_name, _, _ = project_workspace(project_name)
        project_agents[safe_project_name] = AgentLoop()
        _write_chat_history(safe_project_name, [])
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
            items.append({
                "name": path.name,
                "path": str(path),
                "conda_env_dir": str(path / ".slurm-agent" / "conda-env"),
                "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "has_chat": history_path.exists(),
                "has_notes": notes_path.exists(),
            })
        items.sort(key=lambda item: item["updated_at"], reverse=True)
    except Exception as e:
        return JSONResponse({"error": f"读取作业目录列表失败: {e}"}, status_code=500)
    return {"status": "ok", "base_dir": str(projects_base), "projects": items}


@app.get("/api/projects/chat")
async def project_chat_history(project_name: str):
    """Return display chat history for one project session."""
    try:
        safe_project_name, project_dir, _ = project_workspace(project_name)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {
        "status": "ok",
        "project_name": safe_project_name,
        "project_dir": str(project_dir),
        "messages": _read_chat_history(safe_project_name),
    }


@app.post("/api/projects/chat")
async def append_project_chat(req: ProjectChatAppendRequest):
    """Append a display message to one project session history."""
    try:
        safe_project_name, _, _ = project_workspace(req.project_name)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    role = "ai" if req.role == "ai" else "user"
    _append_chat_history(safe_project_name, role, req.content)
    return {"status": "ok", "project_name": safe_project_name}


@app.post("/api/projects")
def create_project(req: ProjectCreateRequest):
    """Create a project directory and its per-project conda environment."""
    try:
        workspace = ensure_project_workspace(req.name)
        notes_path = _append_project_notes(
            workspace.project_dir,
            environment_requirements=req.environment_requirements,
            compute_requirements=req.compute_requirements,
        )
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"创建作业目录失败: {e}"}, status_code=500)

    return {
        "status": "ok",
        "project_name": workspace.project_name,
        "project_dir": str(workspace.project_dir),
        "conda_env_dir": str(workspace.conda_env_dir),
        "conda_created": workspace.conda_created,
        "notes_path": str(notes_path),
    }


@app.post("/api/projects/report")
def project_report(req: ProjectReportRequest):
    """Generate a dependency/environment plan before installing dependencies."""
    try:
        workspace = ensure_project_workspace(req.name)
        notes_path = _append_project_notes(
            workspace.project_dir,
            extra_notes=req.extra_notes,
        )
        scanned_items = scan_project_dependencies(workspace.project_dir)
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
                    {"role": "user", "content": _build_ai_dependency_json_prompt(workspace, scanned_items, req.extra_notes)},
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
def project_job_skeleton(project_name: str):
    """
    下发作业脚本“锁定区”的权威值：真实项目目录、conda 环境路径、
    conda.sh 绝对路径与固定激活前导。前端只展示不可改。
    """
    try:
        workspace = _light_workspace(project_name)
        conda_sh = _conda_sh_path()
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("获取作业脚本骨架失败")
        return JSONResponse({"error": f"获取作业脚本骨架失败: {e}"}, status_code=500)

    prelude = "\n".join([
        "set -euo pipefail",
        "# 运行目录",
        f"cd {shlex.quote(str(workspace.project_dir))}",
        "mkdir -p logs runs",
        "",
        "# 激活项目 Conda 环境",
        "set +u",
        # conda_sh 由服务端解析：真实绝对路径，或 $(conda info --base) 兜底。
        # 后者绝不能 shlex.quote——单引号会禁用 $() 命令替换导致 source 失败
        f"source {conda_sh}",
        f"conda activate {shlex.quote(str(workspace.conda_env_dir))}",
        "set -u",
    ])
    return {
        "status": "ok",
        "project_name": workspace.project_name,
        "project_dir": str(workspace.project_dir),
        "conda_env_dir": str(workspace.conda_env_dir),
        "conda_sh": conda_sh,
        "prelude": prelude,
    }


@app.post("/api/projects/job-body")
def project_job_body(req: JobBodyRequest):
    """一次 LLM 调用生成作业命令正文；#SBATCH 头与目录/环境激活由锁定区负责。"""
    try:
        workspace = _light_workspace(req.name)
        prompt = _build_job_body_prompt(workspace, req.form)
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
    return {
        "status": "ok",
        "project_name": workspace.project_name,
        "body": body,
    }


@app.post("/api/jobs/submit")
def submit_project_job(req: JobSubmitRequest):
    """Save the reviewed sbatch script into the project and submit it via Slurm REST."""
    script = (req.script or "").strip()
    job_name = (req.job_name or "").strip()
    partition = (req.partition or "").strip()
    if not script:
        return JSONResponse({"error": "作业脚本不能为空"}, status_code=400)
    if not job_name:
        return JSONResponse({"error": "作业名不能为空"}, status_code=400)
    if not partition:
        return JSONResponse({"error": "分区不能为空"}, status_code=400)

    # 统一换行为 LF，避免 Windows CRLF 导致集群解析脚本失败
    script = script.replace("\r\n", "\n").replace("\r", "\n")

    try:
        workspace = ensure_project_workspace(req.project_name)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "", job_name).strip(".-") or "job"
        script_path = workspace.project_dir / f"job-{safe_name}.sh"
        script_path.write_text(script, encoding="utf-8")

        client = SlurmClient()
        result = client.submit_job(
            script=script,
            partition=partition,
            name=job_name,
            nodes=max(1, int(req.nodes or 1)),
            time_limit=max(1, int(req.time_limit or 240)),
            extra_job_params={
                # 日志/相对路径以项目目录为基准，而不是服务进程的 cwd
                "current_working_directory": str(workspace.project_dir),
            },
        )
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("提交作业失败")
        return JSONResponse({"error": f"提交作业失败: {e}"}, status_code=500)

    job_id = None
    if isinstance(result, dict):
        for key in ("job_id", "jobid", "id"):
            if result.get(key) is not None:
                job_id = result.get(key)
                break

    return {
        "status": "ok",
        "project_name": workspace.project_name,
        "project_dir": str(workspace.project_dir),
        "conda_env_dir": str(workspace.conda_env_dir),
        "script_path": str(script_path),
        "job_id": job_id,
        "slurm_response": result,
    }


@app.post("/api/files/upload")
async def files_upload(
    project_name: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """
    Receive browser-selected files, package them, store them on the server,
    and verify SHA256 before extracting.

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

            # 打包/落盘/解压/建环境都是阻塞操作，丢到线程池执行，避免冻结事件循环
            result = await asyncio.to_thread(
                package_and_upload, staging_dir, file_count, project_name
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
        "archive_name": result.archive_name,
        "archive_size": result.archive_size,
        "local_sha256": result.local_sha256,
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

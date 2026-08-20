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

import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import getpass
from datetime import datetime
from pathlib import Path
from typing import Optional

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
    ensure_project_workspace,
    find_conda_executable,
    get_max_upload_bytes,
    get_remote_projects_base,
    package_and_upload,
    project_workspace,
    safe_relative_path,
)
from core.dependency_planner import (
    DependencyItem,
    items_to_markdown,
    merge_dependency_items,
    parse_ai_dependency_items,
    precheck_dependencies,
    scan_project_dependencies,
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
8. “将要安装的程序环境列表”必须带版本或版本范围；不知道版本时写“需确认”，不要编造。
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


def _build_ai_dependency_json_prompt(workspace, scanned_items: list[DependencyItem], extra_notes: str = "") -> str:
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
3. 不确定的版本留空，不要编造。
4. 不要返回 python、pip、setuptools、wheel。
5. CUDA/PyTorch/TensorFlow 相关项要保守，版本不确定时写空。
6. 最多返回 20 项。

<已扫描依赖>
{scanned_json}
</已扫描依赖>

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
    commands: list[str] = []
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
                spec = f"{name}{version}"
            elif version.startswith("=="):
                spec = f"{name}={version[2:]}"
            elif version.startswith("="):
                spec = f"{name}{version}"
            elif re.fullmatch(r"[0-9][A-Za-z0-9.*_+!-]*", version):
                spec = f"{name}={version}"
        commands.append(f"{manager} install {shlex.quote(spec)}")
    return commands[:40]


def _validate_installed_items(selected_items: list[dict], conda_env_dir: Path) -> list[dict]:
    if not selected_items:
        return []
    try:
        conda_exe = find_conda_executable()
    except FileTransferError as e:
        return [{
            "name": str(item.get("name") or ""),
            "status": "unknown",
            "detail": str(e),
        } for item in selected_items]

    results: list[dict] = []
    for item in selected_items[:40]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            continue
        try:
            result = subprocess.run(
                [conda_exe, "list", "-p", str(conda_env_dir), name],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            results.append({"name": name, "status": "unknown", "detail": "验证超时"})
            continue
        output = (result.stdout or result.stderr or "").strip()
        found = result.returncode == 0 and any(
            line.split() and line.split()[0].lower().replace("_", "-") == name.lower().replace("_", "-")
            for line in output.splitlines()
            if line and not line.startswith("#")
        )
        results.append({
            "name": name,
            "status": "ok" if found else "missing",
            "detail": _trim_text(output, 1000) or "无输出",
        })
    return results


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
async def slurm_refresh():
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
async def create_project(req: ProjectCreateRequest):
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
async def project_report(req: ProjectReportRequest):
    """Generate a dependency/environment plan before installing dependencies."""
    try:
        workspace = ensure_project_workspace(req.name)
        notes_path = _append_project_notes(
            workspace.project_dir,
            extra_notes=req.extra_notes,
        )
        scanned_items = scan_project_dependencies(workspace.project_dir)
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

        dependency_items = precheck_dependencies(merge_dependency_items(scanned_items + ai_items))
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


@app.post("/api/projects/install-deps")
async def install_project_dependencies(req: ProjectInstallRequest):
    """Run allowed conda/pip install commands from a reviewed dependency plan."""
    try:
        workspace = ensure_project_workspace(req.name)
        selected_items = req.selected_items or []
        commands = _commands_from_selected_items(selected_items) or _extract_install_commands(req.plan)
        if not commands:
            return JSONResponse(
                {"error": "未选择可安装依赖，也未在方案中找到可执行的 conda/mamba/pip install 命令"},
                status_code=400,
            )

        results = []
        for command in commands:
            argv = _normalize_install_command(command, workspace.conda_env_dir)
            result = subprocess.run(
                argv,
                cwd=workspace.project_dir,
                capture_output=True,
                text=True,
                timeout=1200,
            )
            output = _trim_text(((result.stdout or "") + "\n" + (result.stderr or "")).strip(), 4000)
            results.append({
                "command": command,
                "executed": " ".join(shlex.quote(part) for part in argv),
                "returncode": result.returncode,
                "output": output,
            })
            if result.returncode != 0:
                return JSONResponse({
                    "error": f"依赖安装失败：{command}",
                    "status": "failed",
                    "project_name": workspace.project_name,
                    "conda_env_dir": str(workspace.conda_env_dir),
                    "results": results,
                }, status_code=500)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "依赖安装超时，请缩小安装范围后重试"}, status_code=500)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("安装项目依赖失败")
        return JSONResponse({"error": f"安装项目依赖失败: {e}"}, status_code=500)

    validation_results = _validate_installed_items(selected_items, workspace.conda_env_dir)
    return {
        "status": "ok",
        "project_name": workspace.project_name,
        "project_dir": str(workspace.project_dir),
        "conda_env_dir": str(workspace.conda_env_dir),
        "results": results,
        "validation_results": validation_results,
    }


@app.post("/api/jobs/submit")
async def submit_project_job(req: JobSubmitRequest):
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

            result = package_and_upload(staging_dir, file_count, project_name)

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

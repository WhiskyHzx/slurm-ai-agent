#!/usr/bin/env python3
"""Dependency scanning and conservative install planning for project workspaces."""

from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from core.file_transfer import FileTransferError, find_conda_executable, get_conda_channels

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None


# 永远不是依赖包名的词：包管理器自带项 + 常见软件源 channel 名。
# conda install -c bioconda gromacs 里的 "bioconda" 是 channel，不是包。
PACKAGE_NAME_BLOCKLIST = {"python", "pip", "setuptools", "wheel", "conda-forge", "bioconda", "defaults", "pypi"}

TRUSTED_SOURCE_FILES = {
    "requirements.txt",
    "environment.yml",
    "environment.yaml",
    "pyproject.toml",
    "setup.py",
    "Pipfile",
}
MAX_ITEMS = 80
MAX_PRECHECK_ITEMS = 40
MAX_IMPORT_ITEMS = 24
# conda search 冷缓存时要下载 repodata，首次可能要几十秒；8 秒会大面积“预检超时”
CONDA_SEARCH_TIMEOUT = 60
PIP_INDEX_TIMEOUT = 20
# 搜索结果缓存（含版本列表），同一包在 report/install 两个阶段不重复查
SEARCH_CACHE_TTL_SECONDS = 600
_SEARCH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

USER_DEPENDENCY_ALIASES = {
    "gromacs": ("gromacs", "conda"),
    "gmx": ("gromacs", "conda"),
    "lammps": ("lammps", "conda"),
    "cp2k": ("cp2k", "conda"),
    "openmpi": ("openmpi", "conda"),
    "mpi": ("openmpi", "conda"),
    "pytorch": ("pytorch", "conda"),
    "torch": ("pytorch", "conda"),
    "tensorflow": ("tensorflow", "conda"),
    "cuda": ("cuda-toolkit", "conda"),
    "cudatoolkit": ("cudatoolkit", "conda"),
    "numpy": ("numpy", "pip"),
    "scipy": ("scipy", "pip"),
    "pandas": ("pandas", "pip"),
    "matplotlib": ("matplotlib", "pip"),
    "scikit-learn": ("scikit-learn", "pip"),
    "sklearn": ("scikit-learn", "pip"),
}


@dataclass
class DependencyItem:
    name: str
    version: str = ""
    manager: str = "conda"
    source: str = ""
    source_kind: str = "inferred"
    confidence: str = "low"
    selected: bool = False
    reason: str = ""
    precheck_status: str = "unknown"
    precheck_detail: str = ""
    command: str = ""
    # 版本感知预检的结果：软件源里真实存在的版本（逗号分隔，最近优先）与建议版本
    available_versions: str = ""
    suggested_version: str = ""


def _clean_name(value: str) -> str:
    value = value.strip().strip("\"'")
    value = re.sub(r"\[.*?\]", "", value)
    value = re.split(r"[<>=!~; ]", value, 1)[0]
    return value.strip().strip("._-")


def _split_requirement(value: str) -> tuple[str, str]:
    raw = value.strip()
    # Only remove a matching pair that wraps the whole requirement. PEP 508
    # markers often end in a quote (for example: python_version >= '3.11');
    # stripping each edge independently corrupts that valid marker.
    if len(raw) >= 2 and raw[0] in {"\"", "'"} and raw[-1] == raw[0]:
        raw = raw[1:-1].strip()
    raw = raw.split("#", 1)[0].strip()
    if not raw or raw.startswith(("-", "http://", "https://", "git+")):
        return "", ""
    match = re.match(r"^([A-Za-z0-9_.-]+(?:\[[^\]]+\])?)\s*(.*)$", raw)
    if not match:
        return "", ""
    return _clean_name(match.group(1)), match.group(2).strip()


def _make_item(
    name: str,
    version: str,
    manager: str,
    source: str,
    source_kind: str,
    confidence: str,
    reason: str,
) -> DependencyItem | None:
    clean = _clean_name(name)
    if not clean or clean.lower() in PACKAGE_NAME_BLOCKLIST:
        return None
    selected = source_kind == "trusted"
    command = _install_command(clean, version, manager)
    return DependencyItem(
        name=clean,
        version=version.strip(),
        manager=manager,
        source=source,
        source_kind=source_kind,
        confidence=confidence,
        selected=selected,
        reason=reason,
        command=command,
    )


def _install_command(name: str, version: str, manager: str) -> str:
    spec = name
    if version:
        if manager == "pip":
            pinned = version.strip()
            if re.fullmatch(r"[0-9][A-Za-z0-9.*_+!-]*", pinned):
                # 裸版本号必须拼 ==，否则生成 name1.2.3 这样的非法 requirement
                spec = f"{name}=={pinned}"
            else:
                spec = f"{name}{pinned}"
        else:
            pinned = version.strip()
            if pinned.startswith("=="):
                spec = f"{name}={pinned[2:]}"
            elif pinned.startswith("="):
                # 以 = 开头的写法（如 =2026.3）直接拼接
                spec = f"{name}{pinned}"
            elif "=" in pinned:
                # 含 = 但不以 = 开头的是 version=build 三段式（如 2026.3=nompi_cuda），
                # 需要补一个 = 组成 name=version=build
                spec = f"{name}={pinned}"
            elif re.fullmatch(r"[0-9][A-Za-z0-9.*_+!-]*", pinned):
                spec = f"{name}={pinned}"
    if manager == "pip":
        return f"pip install {shlex.quote(spec)}"
    return f"conda install {shlex.quote(spec)}"


def _dedupe(items: list[DependencyItem]) -> list[DependencyItem]:
    merged: dict[tuple[str, str], DependencyItem] = {}
    rank = {"trusted": 3, "script": 2, "ai": 1, "inferred": 0}
    for item in items:
        key = (item.manager, item.name.lower())
        current = merged.get(key)
        if not current or rank.get(item.source_kind, 0) > rank.get(current.source_kind, 0):
            merged[key] = item
        elif current:
            if item.version and not current.version:
                current.version = item.version
                current.command = _install_command(current.name, current.version, current.manager)
            if item.source not in current.source:
                current.source = f"{current.source}; {item.source}"
    return list(merged.values())[:MAX_ITEMS]


def _scan_requirements(path: Path, rel: str) -> list[DependencyItem]:
    items: list[DependencyItem] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        name, version = _split_requirement(line)
        item = _make_item(name, version, "pip", rel, "trusted", "high", "来自 requirements.txt 明确声明")
        if item:
            items.append(item)
    return items


def _scan_environment_yml(path: Path, rel: str) -> list[DependencyItem]:
    items: list[DependencyItem] = []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    in_pip = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "- pip:":
            in_pip = True
            continue
        if not stripped.startswith("- "):
            if not line.startswith(" "):
                in_pip = False
            continue
        value = stripped[2:].strip()
        if value.startswith(("-", "#")):
            continue
        manager = "pip" if in_pip else "conda"
        if manager == "conda" and "=" in value:
            name, version = value.split("=", 1)
            version = "=" + version.strip()
        else:
            name, version = _split_requirement(value)
        item = _make_item(name, version, manager, rel, "trusted", "high", "来自 environment.yml 明确声明")
        if item:
            items.append(item)
    return items


def _scan_pyproject(path: Path, rel: str) -> list[DependencyItem]:
    items: list[DependencyItem] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    if tomllib is None:
        specs = re.findall(r"['\"]([^'\"]+[<>=!~]=?[^'\"]*)['\"]", text)
        for spec in specs:
            name, version = _split_requirement(spec)
            item = _make_item(name, version, "pip", rel, "trusted", "high", "来自 pyproject.toml 明确声明")
            if item:
                items.append(item)
        return items
    try:
        data = tomllib.loads(text)
    except Exception:
        return items
    dependencies = data.get("project", {}).get("dependencies", [])
    optional = data.get("project", {}).get("optional-dependencies", {})
    all_specs = list(dependencies)
    if isinstance(optional, dict):
        for specs in optional.values():
            if isinstance(specs, list):
                all_specs.extend(specs)
    for spec in all_specs:
        if not isinstance(spec, str):
            continue
        name, version = _split_requirement(spec)
        item = _make_item(name, version, "pip", rel, "trusted", "high", "来自 pyproject.toml 明确声明")
        if item:
            items.append(item)
    return items


def _scan_pipfile(path: Path, rel: str) -> list[DependencyItem]:
    items: list[DependencyItem] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    if tomllib is None:
        in_packages = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped in {"[packages]", "[dev-packages]"}:
                in_packages = True
                continue
            if stripped.startswith("["):
                in_packages = False
            if not in_packages or "=" not in stripped:
                continue
            name, version = stripped.split("=", 1)
            version = version.strip().strip("\"'")
            item = _make_item(name.strip(), "" if version == "*" else version, "pip", rel, "trusted", "high", "来自 Pipfile 明确声明")
            if item:
                items.append(item)
        return items
    try:
        data = tomllib.loads(text)
    except Exception:
        return items
    for section in ("packages", "dev-packages"):
        packages = data.get(section, {})
        if not isinstance(packages, dict):
            continue
        for name, spec in packages.items():
            version = "" if spec == "*" else str(spec)
            item = _make_item(name, version, "pip", rel, "trusted", "high", f"来自 Pipfile 的 {section}")
            if item:
                items.append(item)
    return items


def _scan_setup_py(path: Path, rel: str) -> list[DependencyItem]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    items: list[DependencyItem] = []
    for block in re.findall(r"install_requires\s*=\s*\[(.*?)\]", text, flags=re.S):
        for spec in re.findall(r"['\"]([^'\"]+)['\"]", block):
            name, version = _split_requirement(spec)
            item = _make_item(name, version, "pip", rel, "trusted", "high", "来自 setup.py install_requires")
            if item:
                items.append(item)
    return items


def _scan_shell(path: Path, rel: str) -> list[DependencyItem]:
    items: list[DependencyItem] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            parts = shlex.split(stripped)
        except ValueError:
            continue
        lowered = [part.lower() for part in parts]
        if len(parts) < 3:
            continue
        manager = ""
        start = 0
        if lowered[:2] in (["pip", "install"], ["conda", "install"], ["mamba", "install"]):
            manager = "pip" if lowered[0] == "pip" else "conda"
            start = 2
        elif lowered[:4] == ["python", "-m", "pip", "install"]:
            manager = "pip"
            start = 4
        if not manager:
            continue
        skip_next = False
        for token in parts[start:]:
            if skip_next:
                skip_next = False
                continue
            if token in {"-c", "--channel", "-p", "--prefix", "-n", "--name", "-r", "--requirement"}:
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            name, version = _split_requirement(token.replace("=", "==", 1) if manager == "pip" else token)
            item = _make_item(name, version, manager, rel, "trusted", "high", "来自 shell 脚本安装命令")
            if item:
                items.append(item)
    return items


def _scan_python_imports(project_dir: Path) -> list[DependencyItem]:
    items: list[DependencyItem] = []
    seen: set[str] = set()
    stdlib_like = {
        "argparse", "collections", "dataclasses", "datetime", "functools", "itertools",
        "json", "logging", "math", "os", "pathlib", "re", "shutil", "subprocess",
        "sys", "tempfile", "typing", "unittest", "urllib",
    }
    for path in sorted(project_dir.rglob("*.py")):
        rel = path.relative_to(project_dir)
        if ".slurm-agent" in rel.parts or ".git" in rel.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore")[:120000])
        except Exception:
            continue
        for node in ast.walk(tree):
            name = ""
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split(".", 1)[0]
                    if name and name not in seen and name not in stdlib_like:
                        seen.add(name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                name = node.module.split(".", 1)[0]
                if name and name not in seen and name not in stdlib_like:
                    seen.add(name)
            if len(seen) >= MAX_IMPORT_ITEMS:
                break
        if len(seen) >= MAX_IMPORT_ITEMS:
            break
    for name in sorted(seen):
        item = _make_item(name, "", "pip", "Python import 扫描", "inferred", "low", "从源码 import 推断，需用户确认")
        if item:
            items.append(item)
    return items


def scan_project_dependencies(project_dir: Path) -> list[DependencyItem]:
    items: list[DependencyItem] = []
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(project_dir)
        if ".slurm-agent" in rel.parts or ".git" in rel.parts:
            continue
        rel_text = str(rel)
        name = path.name
        try:
            if name == "requirements.txt":
                items.extend(_scan_requirements(path, rel_text))
            elif name in {"environment.yml", "environment.yaml"}:
                items.extend(_scan_environment_yml(path, rel_text))
            elif name == "pyproject.toml":
                items.extend(_scan_pyproject(path, rel_text))
            elif name == "Pipfile":
                items.extend(_scan_pipfile(path, rel_text))
            elif name == "setup.py":
                items.extend(_scan_setup_py(path, rel_text))
            elif path.suffix.lower() in {".sh", ".bash", ".sbatch"}:
                items.extend(_scan_shell(path, rel_text))
        except OSError:
            continue
    items.extend(_scan_python_imports(project_dir))
    return _dedupe(items)


def _version_near_token(text: str, token: str) -> str:
    pattern = rf"\b{re.escape(token)}\b\s*(?:==|=|版本|version)?\s*([0-9][A-Za-z0-9.*_+!-]*(?:=[A-Za-z0-9.*_+!-]+)?)"
    match = re.search(pattern, text, flags=re.I)
    return match.group(1).strip() if match else ""


def scan_user_dependency_notes(text: str) -> list[DependencyItem]:
    """Parse explicit dependency names from user-written requirements.

    This is intentionally deterministic: if the user types "需要 gromacs",
    the dependency should not depend on LLM inference.
    """
    lowered = str(text or "").lower()
    items: list[DependencyItem] = []

    command_pattern = re.compile(
        r"(?:python\s+-m\s+pip|pip|conda|mamba)\s+install\s+([A-Za-z0-9_.=-]+(?:\s+[A-Za-z0-9_.=-]+){0,12})",
        flags=re.I,
    )
    for match in command_pattern.finditer(text or ""):
        command = match.group(0).lower()
        manager = "pip" if "pip" in command else "conda"
        channels: list[str] = []
        # 记录上一个 token 是否为“带值选项”（-c/--channel 的值是软件源名，
        # -p/-n/-r 的值是路径/环境名，都不是包名，不能当成依赖）
        pending_option: str | None = None
        for raw_spec in shlex.split(match.group(1)):
            if pending_option is not None:
                if pending_option == "channel":
                    channels.append(raw_spec)
                pending_option = None
                continue
            if raw_spec in {"-c", "--channel"}:
                pending_option = "channel"
                continue
            if raw_spec in {"-p", "--prefix", "-n", "--name", "-r", "--requirement"}:
                pending_option = "value"
                continue
            if raw_spec.startswith("--channel="):
                channels.append(raw_spec.split("=", 1)[1])
                continue
            if raw_spec.startswith("-"):
                continue
            name, version = _split_requirement(raw_spec)
            reason = "来自用户明确输入的安装需求"
            if channels:
                reason += f"（用户指定 channel：{'、'.join(dict.fromkeys(channels))}）"
            item = _make_item(name, version, manager, "用户输入", "trusted", "high", reason)
            if item:
                item.selected = True
                items.append(item)

    for token, (package, manager) in USER_DEPENDENCY_ALIASES.items():
        if not re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(token)}(?![A-Za-z0-9_.-])", lowered):
            continue
        version = _version_near_token(lowered, token)
        item = _make_item(package, version, manager, "用户输入", "trusted", "high", "来自用户明确输入的依赖需求")
        if item:
            item.selected = True
            items.append(item)

    return _dedupe(items)


def installed_packages_snapshot(conda_env_dir: Path | None) -> dict[str, str]:
    """公开封装：返回项目环境已安装包 {name: version} 快照。"""
    try:
        conda_exe = find_conda_executable()
    except FileTransferError:
        return {}
    return _installed_packages(conda_exe, conda_env_dir)


def _installed_packages(conda_exe: str, conda_env_dir: Path | None) -> dict[str, str]:
    """Snapshot {normalized_name: version} of packages already in the project env."""
    if conda_env_dir is None or not (conda_env_dir / "conda-meta").exists():
        return {}
    try:
        result = subprocess.run(
            [conda_exe, "list", "-p", str(conda_env_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    installed: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            installed[parts[0].lower().replace("_", "-")] = parts[1]
    return installed


def _version_specifier(version: str) -> SpecifierSet | None:
    """Parse a project version requirement according to PEP 440.

    Environment markers belong to the requirement rather than its version
    constraint, so only the part before ``;`` is evaluated here. Bare and
    conda-style exact versions are converted to PEP 440 equality constraints.
    """
    raw = str(version or "").split(";", 1)[0].strip()
    if not raw:
        return SpecifierSet()

    # Conda may express an exact build as ``version=build``. Availability is
    # checked at version level here; build selection is handled separately.
    if not raw.startswith(("<", ">", "!", "~", "=")) and "=" in raw:
        raw = raw.split("=", 1)[0].strip()
    if raw.startswith("=") and not raw.startswith(("==", "===")):
        raw = "==" + raw[1:].strip()
    elif not raw.startswith(("<", ">", "!", "~", "=")):
        raw = "==" + raw

    try:
        return SpecifierSet(raw)
    except InvalidSpecifier:
        return None


def _matching_versions(requested: str, available: list[str]) -> list[str]:
    """Return source versions satisfying the complete requested range."""
    specifier = _version_specifier(requested)
    if specifier is None:
        return []
    valid_candidates: list[str] = []
    for candidate in available:
        try:
            Version(candidate)
        except InvalidVersion:
            continue
        valid_candidates.append(candidate)
    # SpecifierSet.filter applies PEP 440's prerelease fallback semantics.
    return list(specifier.filter(valid_candidates))


def _normalize_requested_version(version: str) -> str:
    """把各种写法归一成纯版本号：'==1.2', '=1.2', '1.2.*' → '1.2'。"""
    v = str(version or "").strip()
    v = v.lstrip("=")
    # 保留 version=build 三段式中的 version 部分
    v = v.split("=", 1)[0]
    v = v.strip(".*")
    return v


def _version_key(version: str):
    """把版本号转成可比较的元组，数值段用 int、非数值段用小写字符串。"""
    parts = re.split(r"[._-]", str(version))
    key = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part.lower()))
    return tuple(key)


def _version_matches(requested: str, available: list[str]) -> bool:
    """Whether at least one source version satisfies the complete requirement."""
    return bool(_matching_versions(requested, available))


def _suggest_version(requested: str, available: list[str]) -> str:
    """从可用版本中挑建议版本：优先同 major.minor，其次同 major，最后最新。"""
    target = _normalize_requested_version(requested)
    if not available:
        return ""
    matching = _matching_versions(requested, available)
    if matching:
        return matching[-1]
    if target:
        segments = re.split(r"[._-]", target)
        if len(segments) >= 2:
            prefix = ".".join(segments[:2])
            same_minor = [v for v in available if v.startswith(prefix + ".") or v == prefix]
            if same_minor:
                return same_minor[-1]
        if segments:
            major = segments[0]
            same_major = [v for v in available if v.split(".", 1)[0] == major]
            if same_major:
                return same_major[-1]
    return available[-1]


def _extract_json_object(text: str) -> Any | None:
    """从 conda --json 输出中提取第一个完整 JSON 对象（前后可能有 WARNING 文本）。"""
    for match in re.finditer(r"\{", text):
        candidate = text[match.start():]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def search_package_versions(
    name: str,
    conda_exe: str | None = None,
    timeout: int = CONDA_SEARCH_TIMEOUT,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    查询 conda-forge 上某包的真实可用版本与构建。

    返回 {"ok": bool, "versions": [...], "builds": [...], "error": str}。
    versions 按版本升序；builds 是最近若干个 "version=build" 字符串。
    结果带 TTL 缓存，report 与 install 阶段复用，避免重复下载 repodata。
    """
    cache_key = str(name or "").strip().lower()
    now = time.monotonic()
    if use_cache and cache_key in _SEARCH_CACHE:
        cached_at, cached = _SEARCH_CACHE[cache_key]
        if now - cached_at < SEARCH_CACHE_TTL_SECONDS:
            return dict(cached)

    result: dict[str, Any] = {"ok": False, "versions": [], "builds": [], "error": ""}
    try:
        conda_exe = conda_exe or find_conda_executable()
    except FileTransferError as e:
        result["error"] = str(e)
        return result

    channel_args = ["--override-channels"]
    for channel in get_conda_channels():
        channel_args.extend(["-c", channel])
    try:
        proc = subprocess.run(
            [conda_exe, "search", "--json", *channel_args, cache_key],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result["error"] = f"conda search 超时（>{timeout}s）"
        return result
    except OSError as e:
        result["error"] = f"conda search 执行失败： {e}"
        return result

    raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
    data = _extract_json_object(raw)
    entries: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if isinstance(data.get("error"), dict):
            result["error"] = str(data["error"].get("message") or data["error"])[:300]
        elif data.get("exception_name") == "PackagesNotFoundError":
            result["error"] = "软件源中未找到该包"
        elif isinstance(data.get(cache_key), list):
            entries = [e for e in data[cache_key] if isinstance(e, dict) and e.get("version")]
    if not entries and not result["error"]:
        # 非 JSON 输出（如 conda tos 提示）也算查询失败
        result["error"] = _trim_search_output(raw)
    if entries:
        # 按版本排序，同版本多个构建保留全部
        entries.sort(key=lambda e: _version_key(str(e.get("version"))))
        versions: list[str] = []
        for entry in entries:
            v = str(entry.get("version"))
            if v not in versions:
                versions.append(v)
        builds = [
            f"{e.get('version')}={e.get('build')}" for e in entries[-24:]
        ]
        result.update({"ok": True, "versions": versions, "builds": builds, "error": ""})

    if use_cache:
        _SEARCH_CACHE[cache_key] = (now, dict(result))
    return result


def _trim_search_output(text: str, limit: int = 300) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned[:limit] or "无输出"


def _parse_pip_available_versions(output: str) -> list[str]:
    """从 `pip index versions` 输出中解析可用版本列表（倒序→升序）。"""
    for line in str(output or "").splitlines():
        if "Available versions:" in line:
            versions = [
                v.strip() for v in line.split("Available versions:", 1)[1].split(",") if v.strip()
            ]
            versions.reverse()
            return versions
    return []


def precheck_dependencies(items: list[DependencyItem], conda_env_dir: Path | None = None) -> list[DependencyItem]:
    """
    预检分三部分：
    1. 是否已安装在项目 Conda 环境（conda list 快照一次，已装项默认不勾选，避免重复安装）；
    2. 软件源里是否可获取（conda search / pip index），并解析真实可用版本；
    3. 版本校验：请求的版本在软件源中不存在时标记 version_mismatch 并给出建议版本，
       避免把其它集群的 module 版本号直接当成 conda 版本导致安装失败。
    """
    try:
        conda_exe = find_conda_executable()
    except FileTransferError as e:
        for item in items:
            item.precheck_status = "unknown"
            item.precheck_detail = str(e)
        return items

    installed = _installed_packages(conda_exe, conda_env_dir)
    env_python = conda_env_dir / "bin" / "python" if conda_env_dir else None

    for item in items[:MAX_PRECHECK_ITEMS]:
        key = item.name.lower().replace("_", "-")
        if key in installed:
            item.precheck_status = "installed"
            item.precheck_detail = f"当前项目环境已安装 {item.name} {installed[key]}，无需重复安装"
            item.selected = False
            continue

        if item.manager == "pip":
            try:
                if env_python is not None and env_python.exists():
                    # 直接用项目环境里的 python 查询，避免 conda run 开销
                    result = subprocess.run(
                        [str(env_python), "-m", "pip", "index", "versions", item.name],
                        capture_output=True,
                        text=True,
                        timeout=PIP_INDEX_TIMEOUT,
                    )
                else:
                    result = subprocess.run(
                        [conda_exe, "run", "python", "-m", "pip", "index", "versions", item.name],
                        capture_output=True,
                        text=True,
                        timeout=PIP_INDEX_TIMEOUT,
                    )
            except subprocess.TimeoutExpired:
                item.precheck_status = "unknown"
                item.precheck_detail = "预检超时"
                continue
            except OSError as e:
                item.precheck_status = "unknown"
                item.precheck_detail = str(e)
                continue

            output = (result.stdout or "").strip()
            versions = _parse_pip_available_versions(output)
            if result.returncode == 0 and versions:
                item.available_versions = ", ".join(versions[-10:])
                if _version_matches(item.version, versions):
                    item.precheck_status = "ok"
                    matching = _matching_versions(item.version, versions)
                    item.precheck_detail = (
                        f"版本要求 {item.version or '(未指定)'} 可满足，"
                        f"匹配范围内最新版本 {matching[-1]}"
                    )
                else:
                    item.precheck_status = "version_mismatch"
                    item.suggested_version = _suggest_version(item.version, versions)
                    item.precheck_detail = (
                        f"请求版本 {item.version or '(空)'} 不存在，可用最新 {versions[-1]}；"
                        f"已建议 {item.suggested_version}"
                    )
            elif result.returncode == 0 and output:
                item.precheck_status = "ok"
                item.precheck_detail = "\n".join(output.splitlines()[:4])[:400]
            else:
                item.precheck_status = "missing"
                item.precheck_detail = "\n".join((output or (result.stderr or "")).splitlines()[:4])[:400] or "无输出"
            continue

        # conda：版本感知搜索
        search = search_package_versions(item.name, conda_exe=conda_exe)
        if not search["ok"]:
            item.precheck_status = "missing" if "未找到该包" in search["error"] else "unknown"
            item.precheck_detail = _trim_search_output(search["error"])
            continue

        versions: list[str] = search["versions"]
        builds: list[str] = search["builds"]
        item.available_versions = ", ".join(versions[-10:])
        if not item.version:
            item.precheck_status = "ok"
            item.precheck_detail = f"软件源可用，最新版本 {versions[-1]}（未指定版本，默认装最新）"
            continue
        if _version_matches(item.version, versions):
            item.precheck_status = "ok"
            matching = _matching_versions(item.version, versions)
            matching_version_set = set(matching)
            # 展示满足约束的最近构建变体（nompi/cuda/openmpi 等）。
            matching_builds = [b for b in builds if b.split("=", 1)[0] in matching_version_set]
            detail = f"版本要求 {item.version} 可满足，匹配范围内最新版本 {matching[-1]}"
            if matching_builds:
                item.precheck_detail = f"{detail}，最近构建：{', '.join(matching_builds)}"
            else:
                item.precheck_detail = detail
        else:
            item.precheck_status = "version_mismatch"
            item.suggested_version = _suggest_version(item.version, versions)
            item.precheck_detail = (
                f"请求版本 {item.version} 在软件源中不存在（最近可用：{item.available_versions}）；"
                f"建议改用 {item.suggested_version}"
            )
    return items


def items_to_markdown(items: list[DependencyItem], summary: str = "") -> str:
    precheck_text = {
        "ok": "可找到",
        "missing": "未确认",
        "installed": "已安装",
        "unknown": "需确认",
        "version_mismatch": "版本不存在",
    }
    lines = ["## 依赖检查结果"]
    if summary.strip():
        lines.extend(["", summary.strip()])
    lines.extend([
        "",
        "| 默认 | 名称 | 版本 | 安装器 | 来源 | 置信度 | 预检 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
    mismatch_lines: list[str] = []
    for item in items:
        selected = "是" if item.selected else "否"
        version = item.version or "需确认"
        precheck = precheck_text.get(item.precheck_status, item.precheck_status)
        lines.append(
            f"| {selected} | `{item.name}` | {version} | {item.manager} | {item.source} | {item.confidence} | {precheck} |"
        )
        if item.precheck_status == "version_mismatch" and item.suggested_version:
            mismatch_lines.append(
                f"- `{item.name}`：请求版本 {item.version or '(空)'} 在软件源中不存在，"
                f"建议改用 {item.suggested_version}（可用版本：{item.available_versions or '见预检详情'}）"
            )
    lines.extend([
        "",
        "可信依赖已默认勾选；已安装项默认不勾选；AI 或源码 import 推断项需要用户在弹窗中确认后再安装。",
    ])
    if mismatch_lines:
        lines.extend(["", "**以下依赖的版本在软件源中不存在，安装前请改用建议版本：**", *mismatch_lines])
    return "\n".join(lines)


def parse_ai_dependency_items(raw: str) -> list[DependencyItem]:
    text = str(raw or "").strip()
    if not text:
        return []
    match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if match:
        text = match.group(1).strip()
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("dependencies", [])
    if not isinstance(data, list):
        return []

    items: list[DependencyItem] = []
    for entry in data[:40]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        version = str(entry.get("version") or "").strip()
        manager = str(entry.get("manager") or "conda").strip().lower()
        if manager not in {"conda", "pip"}:
            manager = "conda"
        reason = str(entry.get("reason") or "AI 根据项目内容推断，需用户确认").strip()
        item = _make_item(name, version, manager, "AI 推断", "ai", "low", reason)
        if item:
            item.selected = False
            items.append(item)
    return items


def serialize_items(items: list[DependencyItem]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]


def merge_dependency_items(items: list[DependencyItem]) -> list[DependencyItem]:
    return _dedupe(items)

#!/usr/bin/env python3
"""Dependency scanning and conservative install planning for project workspaces."""

from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.file_transfer import FileTransferError, find_conda_executable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None


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


def _clean_name(value: str) -> str:
    value = value.strip().strip("\"'")
    value = re.sub(r"\[.*?\]", "", value)
    value = re.split(r"[<>=!~; ]", value, 1)[0]
    return value.strip().strip("._-")


def _split_requirement(value: str) -> tuple[str, str]:
    raw = value.strip().strip("\"'")
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
    if not clean or clean.lower() in {"python", "pip", "setuptools", "wheel"}:
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
            spec = f"{name}{version}"
        else:
            pinned = version.strip()
            if pinned.startswith("=="):
                spec = f"{name}={pinned[2:]}"
            elif pinned.startswith("="):
                spec = f"{name}{pinned}"
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


def precheck_dependencies(items: list[DependencyItem]) -> list[DependencyItem]:
    try:
        conda_exe = find_conda_executable()
    except FileTransferError as e:
        for item in items:
            item.precheck_status = "unknown"
            item.precheck_detail = str(e)
        return items

    for item in items[:MAX_PRECHECK_ITEMS]:
        try:
            if item.manager == "pip":
                result = subprocess.run(
                    [conda_exe, "run", "python", "-m", "pip", "index", "versions", item.name],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
            else:
                result = subprocess.run(
                    [conda_exe, "search", "--override-channels", "-c", "conda-forge", item.name],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
        except subprocess.TimeoutExpired:
            item.precheck_status = "unknown"
            item.precheck_detail = "预检超时"
            continue
        except OSError as e:
            item.precheck_status = "unknown"
            item.precheck_detail = str(e)
            continue

        output = (result.stdout or result.stderr or "").strip()
        item.precheck_status = "ok" if result.returncode == 0 and output else "missing"
        item.precheck_detail = "\n".join(output.splitlines()[:8])[:800] or "无输出"
    return items


def items_to_markdown(items: list[DependencyItem], summary: str = "") -> str:
    lines = ["## 依赖检查结果"]
    if summary.strip():
        lines.extend(["", summary.strip()])
    lines.extend([
        "",
        "| 默认 | 名称 | 版本 | 安装器 | 来源 | 置信度 | 预检 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
    for item in items:
        selected = "是" if item.selected else "否"
        version = item.version or "需确认"
        lines.append(
            f"| {selected} | `{item.name}` | {version} | {item.manager} | {item.source} | {item.confidence} | {item.precheck_status} |"
        )
    lines.extend([
        "",
        "可信依赖已默认勾选；AI 或源码 import 推断项需要用户在弹窗中确认后再安装。",
    ])
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

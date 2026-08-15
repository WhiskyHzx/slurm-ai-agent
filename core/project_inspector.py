#!/usr/bin/env python3
"""
project_inspector.py - Read-only project scanner for job preparation.

The scanner is intentionally conservative:
  - it never writes to user files;
  - it only reads small text-like files;
  - it skips secrets, datasets, model checkpoints, archives, and build outputs;
  - it returns a compact summary suitable for an LLM to draft an sbatch script.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple


TEXT_EXTENSIONS = {
    ".py", ".sh", ".bash", ".md", ".txt", ".toml", ".yaml", ".yml",
    ".json", ".cfg", ".ini", ".conf", ".requirements",
}

IMPORTANT_NAMES = {
    "README", "README.md", "readme.md", "requirements.txt", "pyproject.toml",
    "setup.py", "setup.cfg", "environment.yml", "environment.yaml",
    "train.py", "main.py", "run.py", "run.sh", "submit.sh",
}

SKIP_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".venv", "venv", "env", "node_modules", "data", "datasets", "dataset",
    "checkpoints", "checkpoint", "weights", "runs", "wandb", "outputs",
    "output", "logs", "log", "build", "dist",
}

SENSITIVE_PATTERNS = [
    ".env", "id_rsa", "id_dsa", "id_ed25519", "known_hosts",
    "authorized_keys", "credentials", "secret", "secrets", "token",
    ".pem", ".key", ".crt", ".p12", ".pfx",
]

BINARY_OR_LARGE_EXTENSIONS = {
    ".pt", ".pth", ".ckpt", ".onnx", ".safetensors", ".bin", ".npy",
    ".npz", ".h5", ".hdf5", ".csv", ".tsv", ".parquet", ".arrow",
    ".zip", ".tar", ".gz", ".tgz", ".rar", ".7z", ".png", ".jpg",
    ".jpeg", ".gif", ".bmp", ".mp4", ".avi", ".mov", ".pdf",
}


def inspect_project(
    path: str = ".",
    max_files: int = 12,
    max_bytes_per_file: int = 6000,
    max_tree_entries: int = 120,
) -> str:
    """
    Scan a project directory and return a compact JSON summary.

    Parameters:
        path: project directory to inspect. The scanner only reads files.
        max_files: maximum number of file contents to include.
        max_bytes_per_file: maximum bytes read from each selected file.
        max_tree_entries: maximum file tree entries to include.
    """
    root = Path(os.path.expandvars(path)).expanduser()
    if not root.exists():
        return f"项目路径不存在: {root}"
    if not root.is_dir():
        return f"项目路径不是目录: {root}"

    try:
        root = root.resolve()
    except OSError:
        return f"无法解析项目路径: {root}"

    files, skipped = _walk_project(root, max_tree_entries=max_tree_entries)
    selected = _select_files(files, max_files=max_files)

    contents = []
    for file_path in selected:
        rel = _rel(file_path, root)
        try:
            text, truncated = _read_text_preview(file_path, max_bytes=max_bytes_per_file)
        except OSError as exc:
            skipped.append({"path": rel, "reason": f"读取失败: {exc}"})
            continue
        contents.append({
            "path": rel,
            "truncated": truncated,
            "content": text,
        })

    signals = _infer_signals(root, files, contents)
    result = {
        "project_root": str(root),
        "tree": [_rel(p, root) for p in files[:max_tree_entries]],
        "files_read": [c["path"] for c in contents],
        "files_skipped_sample": skipped[:40],
        "signals": signals,
        "file_contents": contents,
        "safety_note": (
            "本工具只读项目文件；已跳过常见密钥、Token、环境文件、数据集、"
            "模型权重、压缩包和大文件。提交作业前必须让用户检查脚本并确认。"
        ),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


def _walk_project(root: Path, max_tree_entries: int) -> Tuple[List[Path], List[Dict[str, str]]]:
    files: List[Path] = []
    skipped: List[Dict[str, str]] = []

    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        dirnames[:] = [
            d for d in sorted(dirnames)
            if not _should_skip_dir(d, current_path / d, root, skipped)
        ]

        for name in sorted(filenames):
            path = current_path / name
            rel = _rel(path, root)
            if _is_sensitive(path):
                skipped.append({"path": rel, "reason": "敏感文件名，跳过"})
                continue
            if path.suffix.lower() in BINARY_OR_LARGE_EXTENSIONS:
                skipped.append({"path": rel, "reason": "二进制/数据/模型/压缩文件，跳过"})
                continue
            try:
                size = path.stat().st_size
            except OSError:
                skipped.append({"path": rel, "reason": "无法读取元数据，跳过"})
                continue
            if size > 1024 * 1024:
                skipped.append({"path": rel, "reason": "文件超过 1 MB，跳过"})
                continue
            files.append(path)
            if len(files) >= max_tree_entries * 3:
                skipped.append({"path": "...", "reason": "目录文件较多，已停止深度收集"})
                return files, skipped

    return files, skipped


def _should_skip_dir(name: str, path: Path, root: Path, skipped: List[Dict[str, str]]) -> bool:
    if name in SKIP_DIRS or name.startswith("."):
        skipped.append({"path": _rel(path, root), "reason": "跳过目录"})
        return True
    return False


def _select_files(files: List[Path], max_files: int) -> List[Path]:
    def score(path: Path) -> Tuple[int, str]:
        name = path.name
        rel = str(path).replace("\\", "/")
        lower = name.lower()
        value = 0
        if name in IMPORTANT_NAMES or lower in {n.lower() for n in IMPORTANT_NAMES}:
            value += 100
        if lower.startswith("train") or lower.startswith("main") or lower.startswith("run"):
            value += 60
        if "/scripts/" in rel or "\\scripts\\" in str(path):
            value += 30
        if path.suffix.lower() in {".py", ".sh"}:
            value += 20
        if path.suffix.lower() in {".yaml", ".yml", ".json", ".toml"}:
            value += 10
        return (-value, rel)

    text_files = [p for p in files if _looks_readable_text(p)]
    return sorted(text_files, key=score)[:max_files]


def _looks_readable_text(path: Path) -> bool:
    if path.name in IMPORTANT_NAMES:
        return True
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    return False


def _read_text_preview(path: Path, max_bytes: int) -> Tuple[str, bool]:
    data = path.read_bytes()
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    text = _redact_secret_like_lines(text)
    if truncated:
        text += "\n\n...（文件较长，后续内容已截断）"
    return text, truncated


def _redact_secret_like_lines(text: str) -> str:
    redacted = []
    pattern = re.compile(
        r"(api[_-]?key|secret|token|password|passwd|private[_-]?key|access[_-]?key)",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        if pattern.search(line):
            redacted.append("[REDACTED: 该行疑似包含密钥或敏感配置]")
        else:
            redacted.append(line)
    return "\n".join(redacted)


def _is_sensitive(path: Path) -> bool:
    lower = path.name.lower()
    return any(pattern.lower() in lower for pattern in SENSITIVE_PATTERNS)


def _infer_signals(root: Path, files: List[Path], contents: List[Dict[str, Any]]) -> Dict[str, Any]:
    all_text = "\n".join(c["content"] for c in contents).lower()
    rel_files = [_rel(p, root) for p in files]
    py_files = [p for p in rel_files if p.endswith(".py")]

    entry_candidates = [
        p for p in rel_files
        if Path(p).name in {"train.py", "main.py", "run.py"}
        or p.startswith("scripts/")
    ][:8]

    dependency_files = [
        p for p in rel_files
        if Path(p).name in {"requirements.txt", "pyproject.toml", "setup.py", "environment.yml", "environment.yaml"}
    ]

    config_files = [
        p for p in rel_files
        if Path(p).suffix.lower() in {".yaml", ".yml", ".json", ".toml", ".cfg", ".ini"}
    ][:12]

    frameworks = []
    for name, needles in {
        "pytorch": ["import torch", "torch.", "pytorch", "torchrun"],
        "tensorflow": ["tensorflow", "import tf", "keras"],
        "jax": ["import jax", "jax."],
        "sklearn": ["sklearn", "scikit-learn"],
    }.items():
        if any(needle in all_text for needle in needles):
            frameworks.append(name)

    uses_gpu = any(
        needle in all_text
        for needle in ["cuda", "gpu", "torch.cuda", "--gres=gpu", "nvidia-smi"]
    )
    distributed = any(
        needle in all_text
        for needle in ["torchrun", "distributed", "ddp", "nccl", "deepspeed"]
    )

    suggested_commands = []
    for candidate in entry_candidates:
        if candidate.endswith(".py"):
            if "train" in Path(candidate).name:
                suggested_commands.append(f"python {candidate}")
            else:
                suggested_commands.append(f"python {candidate}")
        elif candidate.endswith(".sh"):
            suggested_commands.append(f"bash {candidate}")

    return {
        "python_files_count": len(py_files),
        "entry_candidates": entry_candidates,
        "dependency_files": dependency_files,
        "config_files": config_files,
        "frameworks": frameworks,
        "uses_gpu": uses_gpu,
        "distributed_training_hint": distributed,
        "suggested_commands": suggested_commands[:5],
        "recommended_template_hint": (
            "pytorch_ddp" if distributed and "pytorch" in frameworks
            else "pytorch_single_gpu" if uses_gpu and "pytorch" in frameworks
            else "simple_script"
        ),
    }


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(inspect_project("."))

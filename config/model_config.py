"""Runtime LLM model configuration.

The API key stays in environment variables or .env. This module stores only
non-secret model metadata and the selected model.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from config.settings import LLM_BASE_URL, LLM_MODEL


MODEL_CONFIG_PATH = Path(
    os.environ.get(
        "LLM_MODEL_CONFIG_PATH",
        str(Path(__file__).resolve().parent / "model_config.json"),
    )
)
MODEL_FAMILIES = ("deepseek", "glm")


def _api_key() -> str:
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("环境变量 LLM_API_KEY 未设置，无法获取模型列表。")
    return key


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def load_model_config() -> dict[str, Any]:
    if not MODEL_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_model_config(config: dict[str, Any]) -> None:
    MODEL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _model_id(model: Any) -> str:
    if isinstance(model, dict):
        return str(model.get("id", ""))
    return str(getattr(model, "id", ""))


def _filter_models(model_ids: list[str]) -> dict[str, list[str]]:
    filtered: dict[str, list[str]] = {family: [] for family in MODEL_FAMILIES}
    for model_id in sorted(set(model_ids), key=str.lower):
        lowered = model_id.lower()
        for family in MODEL_FAMILIES:
            if family in lowered:
                filtered[family].append(model_id)
                break
    return filtered


def _flatten_models(families: dict[str, list[str]]) -> list[str]:
    models: list[str] = []
    for family in MODEL_FAMILIES:
        models.extend(families.get(family, []))
    return models


def refresh_model_config() -> dict[str, Any]:
    api_key = _api_key()
    client = OpenAI(api_key=api_key, base_url=LLM_BASE_URL)
    models = client.models.list()
    model_ids = [_model_id(model) for model in getattr(models, "data", [])]
    families = _filter_models([model_id for model_id in model_ids if model_id])
    available = _flatten_models(families)

    current = load_model_config()
    selected = str(current.get("selected_model") or os.environ.get("LLM_MODEL") or LLM_MODEL)
    if selected not in available:
        selected = next((model for model in available if "deepseek" in model.lower()), "")
    if not selected and available:
        selected = available[0]
    if not selected:
        selected = os.environ.get("LLM_MODEL") or LLM_MODEL

    config = {
        "base_url": LLM_BASE_URL,
        "key_fingerprint": _key_fingerprint(api_key),
        "selected_model": selected,
        "families": families,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_model_config(config)
    return config


def ensure_model_config_current() -> dict[str, Any]:
    api_key = _api_key()
    config = load_model_config()
    if config.get("key_fingerprint") != _key_fingerprint(api_key):
        return refresh_model_config()
    if not config.get("families"):
        return refresh_model_config()
    return config


def get_selected_model() -> str:
    config = load_model_config()
    return str(config.get("selected_model") or os.environ.get("LLM_MODEL") or LLM_MODEL)


def set_selected_model(model: str) -> dict[str, Any]:
    model = model.strip()
    if not model:
        raise RuntimeError("模型名称不能为空。")
    config = ensure_model_config_current()
    available = set(_flatten_models(config.get("families", {})))
    if available and model not in available:
        raise RuntimeError(f"模型不在当前 Key 可用列表中：{model}")
    config["selected_model"] = model
    config["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_model_config(config)
    return config

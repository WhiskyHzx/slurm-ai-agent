#!/usr/bin/env python3
"""
template_engine.py — 作业脚本模板引擎。

从 config/templates/*.json 加载模板，支持：
  - 列出所有可用模板（名称、描述、参数）
  - 按 ID 加载单个模板
  - 根据参数渲染生成完整的 sbatch 脚本
  - 参数校验（类型、范围、必填）

模板语法：
  {{ var_name }}        → 简单变量替换
  {% if cond %}...{% endif %}  → 条件块
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "config" / "templates"

# =========================================================================
# 模板加载
# =========================================================================


def list_templates() -> List[Dict[str, Any]]:
    """
    列出所有可用模板的摘要信息（名称、描述、参数列表）。

    返回 index.json 的内容，不加载完整模板细节。
    """
    index_path = TEMPLATES_DIR / "index.json"
    if not index_path.exists():
        logger.warning("模板索引文件不存在: %s", index_path)
        return []
    with open(index_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_template(template_id: str) -> Optional[Dict[str, Any]]:
    """
    加载指定模板的完整定义（含 script_template）。

    返回 None 如果模板不存在。
    """
    # 先从 index 找文件名
    templates = list_templates()
    entry = next((t for t in templates if t["id"] == template_id), None)
    if not entry:
        logger.warning("模板不存在: %s", template_id)
        return None

    file_path = TEMPLATES_DIR / entry["file"]
    if not file_path.exists():
        logger.warning("模板文件不存在: %s", file_path)
        return None

    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


# =========================================================================
# 模板渲染
# =========================================================================


def render(template_id: str, params: Dict[str, Any]) -> Tuple[str, List[str]]:
    """
    渲染模板，生成完整的 sbatch 脚本。

    参数:
        template_id: 模板 ID（如 "pytorch_single_gpu"）
        params: 用户提供的参数，如 {"job_name": "test", "gpu_count": 2, ...}

    返回:
        (script_text, warnings) — 生成的脚本和警告列表
    """
    template = load_template(template_id)
    if not template:
        raise ValueError(f"模板 '{template_id}' 不存在")

    # 1. 合并默认值
    merged = _apply_defaults(template["parameters"], params)

    # 2. 校验参数
    warnings = _validate(template["parameters"], merged)

    # 3. 渲染
    script_template = template["script_template"]
    script = _render_template(script_template, merged)

    # 4. 附加 notes（如果有）
    notes = template.get("notes", "")
    if notes:
        # 也渲染 notes 中的变量
        notes = _render_simple(notes, merged)
        script = f"# ===== 使用说明 =====\n# {notes}\n# =====================\n\n{script}"

    return script, warnings


def _apply_defaults(
    param_defs: Dict[str, Any], user_params: Dict[str, Any]
) -> Dict[str, Any]:
    """将用户参数与默认值合并。"""
    merged = {}
    for key, definition in param_defs.items():
        if key in user_params:
            merged[key] = user_params[key]
        elif "default" in definition:
            merged[key] = definition["default"]
        else:
            merged[key] = None
    return merged


def _validate(
    param_defs: Dict[str, Any], params: Dict[str, Any]
) -> List[str]:
    """校验参数，返回警告列表。"""
    warnings = []

    for key, definition in param_defs.items():
        value = params.get(key)

        # 必填检查
        if value is None:
            warnings.append(f"参数 '{key}' 未提供值，使用默认值")
            continue

        ptype = definition["type"]

        if ptype == "int":
            try:
                ivalue = int(value)
                params[key] = ivalue  # 修正类型
                if "min" in definition and ivalue < definition["min"]:
                    warnings.append(
                        f"参数 '{key}'={ivalue} 小于最小值 {definition['min']}，"
                        f"已调整为 {definition['min']}"
                    )
                    params[key] = definition["min"]
                if "max" in definition and ivalue > definition["max"]:
                    warnings.append(
                        f"参数 '{key}'={ivalue} 超过最大值 {definition['max']}，"
                        f"已调整为 {definition['max']}"
                    )
                    params[key] = definition["max"]
            except (ValueError, TypeError):
                warnings.append(f"参数 '{key}' 应为整数，实际为 '{value}'")

        elif ptype == "choice":
            choices = definition.get("choices", [])
            if choices and str(value) not in choices:
                warnings.append(
                    f"参数 '{key}'='{value}' 不在可选范围 {choices} 中"
                )

    return warnings


def _render_template(template: str, params: Dict[str, Any]) -> str:
    """
    渲染模板：先处理 {% if %} 条件块，再替换 {{ var }}。

    支持的语法：
      {% if var %}...{% endif %}     — var 为 truthy 时保留内容
      {% if var > 0 %}...{% endif %} — 支持简单比较（>, <, ==, !=）
    """
    result = template

    # 1. 处理 {% if cond %}...{% endif %}
    def _eval_if(match):
        cond = match.group(1).strip()
        body = match.group(2)

        # 解析条件
        # 支持: var, var > N, var < N, var == "str", var != "str"
        cmp_match = re.match(
            r'(\w+)\s*(>|<|==|!=)\s*(.+)', cond
        )
        if cmp_match:
            var_name = cmp_match.group(1)
            op = cmp_match.group(2)
            rhs = cmp_match.group(3).strip().strip('"').strip("'")

            var_value = params.get(var_name)

            try:
                rhs_num = int(rhs)
                var_num = int(var_value) if var_value is not None else 0
                if op == ">":
                    return body if var_num > rhs_num else ""
                elif op == "<":
                    return body if var_num < rhs_num else ""
                elif op == "==":
                    return body if str(var_value) == rhs else ""
                elif op == "!=":
                    return body if str(var_value) != rhs else ""
            except (ValueError, TypeError):
                # 字符串比较
                if op == "==":
                    return body if str(var_value) == rhs else ""
                elif op == "!=":
                    return body if str(var_value) != rhs else ""
                return ""
        else:
            # 简单 truthy 检查
            var_value = params.get(cond)
            if var_value:
                # 对于数字 0，视为 falsy
                if isinstance(var_value, (int, float)) and var_value == 0:
                    return ""
                # 对于字符串 "0" 或 "no" 或 "false"，视为 falsy
                if str(var_value).lower() in ("0", "no", "false", ""):
                    return ""
                return body
            return ""

    result = re.sub(
        r'\{%\s*if\s+(.+?)\s*%\}(.*?)\{%\s*endif\s*%\}',
        _eval_if,
        result,
        flags=re.DOTALL,
    )

    # 2. 替换 {{ var }}
    def _replace_var(match):
        var_name = match.group(1).strip()
        value = params.get(var_name, "")
        return str(value)

    result = re.sub(r'\{\{\s*(\w+)\s*\}\}', _replace_var, result)

    return result


def _render_simple(template: str, params: Dict[str, Any]) -> str:
    """简单变量替换（不处理 {% if %}）。"""
    def _replace_var(match):
        var_name = match.group(1).strip()
        return str(params.get(var_name, ""))
    return re.sub(r'\{\{\s*(\w+)\s*\}\}', _replace_var, template)


# =========================================================================
# 格式化（用于注入 LLM prompt）
# =========================================================================


def format_templates_for_llm() -> str:
    """
    将所有模板的摘要信息格式化为 LLM 可读的文本，
    用于注入 system prompt 或 tool description。
    """
    templates = list_templates()
    if not templates:
        return "（无可用模板）"

    lines = ["可用作业脚本模板：\n"]
    for t in templates:
        lines.append(f"- **{t['display_name']}** (`{t['id']}`)")
        lines.append(f"  {t['description']}")
        lines.append(f"  难度: {t['difficulty']} | 标签: {', '.join(t['tags'])}")
        lines.append("")
    return "\n".join(lines)


def format_template_detail(template_id: str) -> str:
    """
    格式化单个模板的详细信息（参数列表），
    用于 LLM 理解模板参数。
    """
    template = load_template(template_id)
    if not template:
        return f"模板 '{template_id}' 不存在"

    lines = [
        f"## {template['display_name']} (`{template_id}`)",
        f"{template['description']}",
        "",
        "参数：",
    ]
    for key, definition in template["parameters"].items():
        ptype = definition["type"]
        default = definition.get("default", "（必填）")
        desc = definition.get("description", "")

        if ptype == "choice":
            choices = definition.get("choices", [])
            lines.append(
                f"  - **{key}** ({definition['label']}): {ptype}, "
                f"可选 {choices}, 默认 `{default}`"
            )
        elif ptype == "int":
            pmin = definition.get("min", "")
            pmax = definition.get("max", "")
            lines.append(
                f"  - **{key}** ({definition['label']}): {ptype}, "
                f"范围 [{pmin}, {pmax}], 默认 `{default}`"
            )
        else:
            lines.append(
                f"  - **{key}** ({definition['label']}): {ptype}, "
                f"默认 `{default}`"
            )
        if desc:
            lines.append(f"    {desc}")

    if template.get("notes"):
        lines.append(f"\n注意事项: {template['notes']}")

    return "\n".join(lines)


# =========================================================================
# __main__ 测试
# =========================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("template_engine.py 测试")
    print("=" * 60)

    # 1. 列出所有模板
    print("\n--- 模板列表 ---")
    print(format_templates_for_llm())

    # 2. 渲染一个模板
    print("\n--- 渲染测试: pytorch_single_gpu ---")
    script, warnings = render("pytorch_single_gpu", {
        "job_name": "test_train",
        "partition": "P107-RTX5090",
        "gpu_count": 1,
        "time_hours": 2,
        "conda_env": "dl",
        "train_script": "train.py",
        "work_dir": "$HOME/test",
    })
    if warnings:
        print("⚠ 警告:", warnings)
    print(script)

    # 3. 测试条件渲染 (gpu_count=0 时不输出 --gpus)
    print("\n--- 渲染测试: simple_script (无 GPU) ---")
    script, warnings = render("simple_script", {
        "job_name": "cpu_only",
        "partition": "CPU-6530",
        "gpu_count": 0,
        "cpu_count": 4,
        "time_hours": 1,
        "conda_env": "base",
        "command": "python -c 'print(1+1)'",
        "work_dir": "$HOME",
    })
    print(script)

    # 4. 测试参数校验
    print("\n--- 校验测试: 超出范围的 gpu_count ---")
    script, warnings = render("pytorch_single_gpu", {
        "job_name": "bad",
        "gpu_count": 999,
    })
    print("⚠ 警告:", warnings)

    # 5. 显示单个模板详情
    print("\n--- 模板详情: jupyter_interactive ---")
    print(format_template_detail("jupyter_interactive"))
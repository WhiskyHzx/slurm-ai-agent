#!/usr/bin/env python3
"""
tools_registry.py — 工具注册表。

把 core/slurm_client.py 中的每个公开函数包装成 OpenAI 兼容的
Function Calling tool 定义，并提供统一的执行调度入口。

新增工具只需：
  1. 在 TOOL_DEFINITIONS 中添加 name/description/parameters
  2. 在 _execute() 中添加映射分支
"""

import logging
from typing import Dict, Any, List, Optional

from core.slurm_client import SlurmClient

logger = logging.getLogger(__name__)

# =========================================================================
# 工具定义（OpenAI Function Calling 格式）
# =========================================================================

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_jobs",
            "description": (
                "查询算力平台上的作业列表，可按分区过滤。"
                "当用户询问'有哪些作业''查看作业''某分区有什么作业'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "partition": {
                        "type": "string",
                        "description": (
                            "分区名，如 P107-RTX5090、P107-A100、GPU-RTX5090、"
                            "GPU-A100、CPU-6530、CPU-8358P、Students。不传则返回全部。"
                        ),
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job",
            "description": (
                "查询单个作业的详细信息，包括状态、分区、申请资源、运行时间等。"
                "当用户询问'某个作业的详情''作业12345是什么状态'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "作业 ID，如 12345",
                    }
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_job",
            "description": (
                "向算力平台提交一个作业。"
                "当用户说'提交作业''帮我跑一个任务''生成并提交脚本'时调用。"
                "需要提供作业脚本内容和目标分区等信息。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": (
                            "作业脚本内容，必须是完整的 bash 脚本，"
                            "以 #!/bin/bash 开头，包含 srun 或 其他命令。"
                        ),
                    },
                    "partition": {
                        "type": "string",
                        "description": "目标分区名，默认 P107-RTX5090",
                    },
                    "name": {
                        "type": "string",
                        "description": "作业名称，用于在队列中标识",
                    },
                    "nodes": {
                        "type": "integer",
                        "description": "申请节点数，默认 1",
                    },
                    "time_limit": {
                        "type": "integer",
                        "description": "运行时间上限（分钟），默认 60",
                    },
                },
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_job",
            "description": (
                "取消（删除）一个已提交的作业。"
                "当用户说'取消作业''删除作业''停止作业12345'时调用。"
                "注意：取消操作不可逆，执行前应向用户确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "要取消的作业 ID",
                    }
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diag",
            "description": (
                "查看集群整体统计信息，包括运行中作业数、排队作业数、"
                "节点状态等。当用户询问'集群状态''平台忙不忙''有多少节点'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_nodes",
            "description": (
                "查询集群所有计算节点的详细信息，包括节点状态、CPU/GPU 资源等。"
                "当用户询问'有哪些节点''节点配置''GPU 型号'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_qos",
            "description": (
                "查询所有 QoS（服务质量）配置及资源配额限制，"
                "包括每个 QoS 允许的最大 CPU 核数、GPU 卡数、内存等。"
                "当用户询问'我能用多少资源''配额是多少''QoS 限制'时调用。"
                "提交作业前也应调用此工具检查配额是否足够。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_jobs_history",
            "description": (
                "查询历史作业记录（含已完成、失败、取消的作业）。"
                "当用户询问'之前的作业为什么失败了''历史作业'"
                "'查看已完成作业'时调用。可用于报错诊断。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "可选，按作业 ID 精确查询某个历史作业",
                    },
                },
                "required": [],
            },
        },
    },
    # ---- 以下为 CLI 只读诊断工具（core/cli_executor.py，仅登录节点可用） ----
    {
        "type": "function",
        "function": {
            "name": "get_job_priority",
            "description": (
                "查看排队作业的调度优先级构成（Slurm 命令 sprio）。"
                "当用户询问'作业为什么一直排队''排队优先级''为什么还没轮到我'时调用。"
                "默认查询当前用户的排队作业，也可指定 job_id 或 user。只读查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "可选，只查看指定作业的优先级",
                    },
                    "user": {
                        "type": "string",
                        "description": "可选，用户名，默认当前用户",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shares",
            "description": (
                "查看账户的 fairshare 公平份额和资源使用量（Slurm 命令 sshare）。"
                "当用户询问'公平份额''fairshare''配额是不是被别人占了'时调用。"
                "默认查询当前用户。只读查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user": {
                        "type": "string",
                        "description": "可选，用户名，默认当前用户",
                    },
                    "account": {
                        "type": "string",
                        "description": "可选，账户名，如 competition、stu",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_live_stats",
            "description": (
                "查看正在运行的作业的实时资源占用，如内存、CPU（Slurm 命令 sstat）。"
                "当用户询问'我的作业现在用了多少内存''运行中作业的资源占用'时调用。"
                "只能查询 RUNNING 状态的作业。只读查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "作业 ID，必须是正在运行的作业",
                    }
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_usage_report",
            "description": (
                "查看集群或用户用量统计报表（Slurm 命令 sreport）。"
                "当用户询问'这个月用了多少资源''集群利用率''用量统计'时调用。"
                "report_type=cluster 查集群整体利用率，user_top 查用量最多的用户排行。"
                "只读查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "enum": ["cluster", "user_top"],
                        "description": "报表类型：cluster=集群整体利用率（默认），user_top=用量最多的用户排行",
                    },
                    "days": {
                        "type": "integer",
                        "description": "统计最近 N 天，默认 30，范围 1-365",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_account",
            "description": (
                "查看当前用户自己的账户（Account）、所属集群和可用 QoS 授权"
                "（Slurm 命令 sacctmgr show assoc）。"
                "当用户询问'我有哪些账户''我能用哪些 QoS''我的账号信息'时调用。"
                "只能查询当前用户自己。只读查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "搜索平台知识库，获取算力平台使用文档中的相关内容。"
                "当用户询问平台使用方法、常见问题、环境配置、作业脚本编写、"
                "错误排查等平台相关知识时调用。"
                "例如：'怎么用 conda''如何提交作业''OOM 怎么办''怎么申请 GPU'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或问题，如'conda 环境配置''作业排队原因'",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_templates",
            "description": (
                "列出所有可用的作业脚本模板。"
                "当用户说'有哪些模板''帮我生成脚本'但不确定用什么模板时，"
                "先调用此工具查看可选模板列表。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_script",
            "description": (
                "根据指定模板和参数生成 Slurm 作业脚本（sbatch 脚本）。"
                "当用户说'帮我生成一个训练脚本''写一个 sbatch 脚本'"
                "'生成作业脚本'时调用。"
                "生成前应先调用 list_templates 查看可用模板，"
                "然后与用户确认模板选择和参数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": (
                            "模板 ID，从 list_templates 返回的列表中选择。"
                            "如 pytorch_single_gpu、simple_script、cpu_batch 等。"
                        ),
                    },
                    "job_name": {
                        "type": "string",
                        "description": "作业名称",
                    },
                    "partition": {
                        "type": "string",
                        "description": "分区名，如 P107-RTX5090、CPU-6530",
                    },
                    "account": {
                        "type": "string",
                        "description": (
                            "计费账户，与分区匹配：P107 系列用 competition，"
                            "Students 用 stu。不传则用模板默认值"
                        ),
                    },
                    "qos": {
                        "type": "string",
                        "description": (
                            "QoS 名称，与分区匹配：qos_p107-rtx5090 / "
                            "qos_p107-a100 / qos_stu_default。不传则用模板默认值"
                        ),
                    },
                    "gpu_count": {
                        "type": "integer",
                        "description": "GPU 数量，0 表示纯 CPU",
                    },
                    "cpu_count": {
                        "type": "integer",
                        "description": "CPU 核数",
                    },
                    "time_hours": {
                        "type": "integer",
                        "description": "运行时间（小时）",
                    },
                    "conda_env": {
                        "type": "string",
                        "description": (
                            "conda 环境名或环境绝对路径。"
                            "若用户在本平台创建了项目，优先传项目环境路径："
                            "$HOME/projects/<项目名>/.slurm-agent/conda-env"
                        ),
                    },
                    "command": {
                        "type": "string",
                        "description": "要执行的命令（用于 simple_script、cpu_batch 等模板）",
                    },
                    "train_script": {
                        "type": "string",
                        "description": "训练脚本路径（用于 PyTorch 模板）",
                    },
                    "work_dir": {
                        "type": "string",
                        "description": "工作目录路径",
                    },
                    "extra_args": {
                        "type": "string",
                        "description": "传递给脚本的额外参数",
                    },
                },
                "required": ["template_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_job_log",
            "description": (
                "读取作业的输出日志文件内容（标准输出或标准错误）。"
                "当用户说'查看作业日志''看看输出''报了什么错'"
                "'作业的输出是什么'时调用。"
                "只能读取当前用户有权限访问的日志文件。"
                "默认读取最后 100 行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "作业 ID",
                    },
                    "log_type": {
                        "type": "string",
                        "enum": ["stdout", "stderr"],
                        "description": (
                            "日志类型：stdout=标准输出（作业的正常输出），"
                            "stderr=标准错误（错误信息和报错）。默认 stdout。"
                        ),
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "读取文件尾部行数，默认 100。如果日志很长可适当增大。",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
]

# =========================================================================
# 工具名 → 简短描述（用于 system prompt 注入）
# =========================================================================

TOOL_DESCRIPTIONS = {
    "list_jobs": "查询作业列表，可按分区过滤",
    "get_job": "查询单个作业详情",
    "submit_job": "提交新作业",
    "cancel_job": "取消指定作业",
    "get_diag": "查看集群整体统计",
    "get_nodes": "查询节点信息",
    "get_qos": "查询 QoS 资源配额",
    "get_jobs_history": "查询历史作业（含已完成/失败）",
    "get_job_priority": "查看排队作业的调度优先级（只读）",
    "get_shares": "查看公平份额 fairshare（只读）",
    "get_job_live_stats": "查看运行中作业的实时资源占用（只读）",
    "get_usage_report": "查看集群/用户用量报表（只读）",
    "get_my_account": "查看自己的账户与 QoS 授权（只读）",
    "search_knowledge": "搜索平台知识库",
    "list_templates": "列出可用作业脚本模板",
    "generate_script": "根据模板生成 sbatch 脚本",
    "read_job_log": "读取作业的输出/错误日志文件",
}


# =========================================================================
# 工具执行调度
# =========================================================================


class ToolExecutor:
    """工具执行器：根据工具名和参数调用对应的 slurm_client 函数。"""

    def __init__(self, client: Optional[SlurmClient] = None):
        self.client = client or SlurmClient()

    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        执行指定工具，返回结果的字符串表示。

        参数:
            tool_name: 工具名称（与 TOOL_DEFINITIONS 中的 name 一致）
            arguments: LLM 给出的参数字典

        返回:
            工具执行结果的字符串（JSON 格式或纯文本），可直接追加到 messages。
        """
        logger.info("执行工具: %s(%s)", tool_name, arguments)

        try:
            if tool_name == "list_jobs":
                result = self.client.list_jobs(
                    partition=arguments.get("partition")
                )
                return self._format_jobs_result(result)

            elif tool_name == "get_job":
                result = self.client.get_job(
                    job_id=arguments["job_id"]
                )
                return self._format_json(result)

            elif tool_name == "submit_job":
                result = self.client.submit_job(
                    script=arguments["script"],
                    partition=arguments.get("partition", "P107-RTX5090"),
                    name=arguments.get("name", "api-job"),
                    nodes=arguments.get("nodes", 1),
                    time_limit=arguments.get("time_limit", 60),
                )
                job_id = result.get("job_id") or result.get("result", {}).get("job_id")
                return (
                    f"作业提交成功！job_id={job_id}。"
                    f"可以使用 get_job({job_id}) 查看详情，"
                    f"或 cancel_job({job_id}) 取消。"
                )

            elif tool_name == "cancel_job":
                result = self.client.cancel_job(
                    job_id=arguments["job_id"]
                )
                return f"作业 {arguments['job_id']} 已成功取消。"

            elif tool_name == "get_diag":
                result = self.client.get_diag()
                return self._format_json(result)

            elif tool_name == "get_nodes":
                result = self.client.get_nodes()
                return self._format_nodes_result(result)

            elif tool_name == "get_qos":
                result = self.client.get_qos()
                return self._format_qos_result(result)

            elif tool_name == "get_jobs_history":
                params = {}
                if arguments.get("job_id"):
                    params["job_id"] = arguments["job_id"]
                result = self.client.get_jobs_history(params=params if params else None)
                return self._format_jobs_result(result)

            elif tool_name == "get_job_priority":
                from core.cli_executor import SlurmCLI
                return SlurmCLI().priority(
                    job_id=arguments.get("job_id"),
                    user=arguments.get("user"),
                )

            elif tool_name == "get_shares":
                from core.cli_executor import SlurmCLI
                return SlurmCLI().shares(
                    user=arguments.get("user"),
                    account=arguments.get("account"),
                )

            elif tool_name == "get_job_live_stats":
                from core.cli_executor import SlurmCLI
                return SlurmCLI().job_live_stats(
                    job_id=arguments["job_id"],
                )

            elif tool_name == "get_usage_report":
                from core.cli_executor import SlurmCLI
                return SlurmCLI().usage_report(
                    report_type=arguments.get("report_type", "cluster"),
                    days=arguments.get("days", 30),
                )

            elif tool_name == "get_my_account":
                from core.cli_executor import SlurmCLI
                return SlurmCLI().my_associations()

            elif tool_name == "search_knowledge":
                from core.knowledge_base import search
                query = arguments.get("query", "")
                if not query:
                    return "（search_knowledge 需要提供 query 参数）"
                return search(query)

            elif tool_name == "list_templates":
                from core.template_engine import format_templates_for_llm
                return format_templates_for_llm()

            elif tool_name == "generate_script":
                from core.template_engine import render, format_template_detail
                template_id = arguments.get("template_id", "")
                if not template_id:
                    return "（generate_script 需要提供 template_id 参数）"

                # 构建参数字典（去掉 template_id 本身和空值）
                script_params = {
                    k: v for k, v in arguments.items()
                    if k != "template_id" and v is not None
                }

                script, warnings = render(template_id, script_params)
                result = f"## 模板: {template_id}\n\n"
                if warnings:
                    result += "⚠ 参数警告:\n"
                    for w in warnings:
                        result += f"  - {w}\n"
                    result += "\n"
                result += f"```bash\n{script}\n```"
                return result

            elif tool_name == "read_job_log":
                return self.client.read_job_log(
                    job_id=arguments["job_id"],
                    log_type=arguments.get("log_type", "stdout"),
                    tail_lines=arguments.get("tail_lines", 100),
                )

            else:
                return f"未知工具: {tool_name}"

        except Exception as e:
            logger.error("工具执行失败: %s", e)
            return f"工具 {tool_name} 执行出错: {e}"

    # ---- 结果格式化 ----

    @staticmethod
    def _format_json(data: Dict[str, Any]) -> str:
        """将 dict 转为紧凑 JSON 字符串。"""
        import json
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)

    @staticmethod
    def _format_jobs_result(data: Dict[str, Any]) -> str:
        """格式化作业列表，提取关键字段，减少 token 消耗。"""
        jobs = data.get("jobs", [])
        if not jobs:
            return "当前没有作业。"

        import json
        summary = []
        for j in jobs:
            summary.append({
                "job_id": j.get("job_id"),
                "name": j.get("name"),
                "partition": j.get("partition"),
                "state": j.get("job_state"),
                "nodes": j.get("nodes"),
                "time_limit": j.get("time_limit"),
                "submit_time": j.get("submit_time"),
            })
        return json.dumps(summary, indent=2, ensure_ascii=False, default=str)

    @staticmethod
    def _format_nodes_result(data: Dict[str, Any]) -> str:
        """格式化节点列表，提取关键字段。"""
        nodes = data.get("nodes", [])
        if not nodes:
            return "未获取到节点信息。"

        import json
        summary = []
        for n in nodes:
            summary.append({
                "name": n.get("name"),
                "state": n.get("state"),
                "cpus": n.get("cpus"),
                "memory": n.get("real_memory"),
                "gres": n.get("gres"),
                "partition": n.get("partition"),
            })
        return json.dumps(summary, indent=2, ensure_ascii=False, default=str)

    @staticmethod
    def _format_qos_result(data: Dict[str, Any]) -> str:
        """格式化 QoS 列表，提取配额关键字段。"""
        qos_list = data.get("qos", [])
        if not qos_list:
            return "未获取到 QoS 信息。"

        import json
        summary = []
        for q in qos_list:
            limits = q.get("limits", {})
            max_tres = (
                limits.get("max", {})
                .get("tres", {})
                .get("per", {})
                .get("user", [])
            )
            # 提取 CPU/GPU/内存限制
            tres_map = {}
            for t in max_tres:
                tres_type = t.get("type", "?")
                tres_name = t.get("name", "")
                count = t.get("count", 0)
                key = f"{tres_type}:{tres_name}" if tres_name else tres_type
                tres_map[key] = count
            summary.append({
                "name": q.get("name"),
                "max_cpu": tres_map.get("cpu", "无限制"),
                "max_gpu": tres_map.get("gres:gpu", "无限制"),
                "max_mem_mb": tres_map.get("mem", "无限制"),
                "preempt_mode": q.get("preempt", {}).get("mode", []),
            })
        return json.dumps(summary, indent=2, ensure_ascii=False, default=str)


# =========================================================================
# __main__ 测试
# =========================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("tools_registry.py 测试")
    print("=" * 60)

    executor = ToolExecutor()

    # 列出所有注册的工具
    print("\n已注册的工具:")
    for t in TOOL_DEFINITIONS:
        name = t["function"]["name"]
        desc = TOOL_DESCRIPTIONS.get(name, "")
        print(f"  • {name}: {desc}")

    # 测试执行 list_jobs
    print("\n[测试] 执行 list_jobs()...")
    try:
        result = executor.execute("list_jobs", {})
        print(f"  结果: {result[:300]}...")
        print("  ✓ 通过")
    except Exception as e:
        print(f"  ✗ 失败: {e}")

    # 测试执行 get_diag
    print("\n[测试] 执行 get_diag()...")
    try:
        result = executor.execute("get_diag", {})
        print(f"  结果: {result[:300]}...")
        print("  ✓ 通过")
    except Exception as e:
        print(f"  ✗ 失败: {e}")

    print("\n✓ tools_registry.py 测试完成")
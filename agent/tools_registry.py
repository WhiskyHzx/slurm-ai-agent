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
from typing import Callable, Dict, Any, List, Optional

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
                "查询算力平台上当前的实时作业列表快照，可按分区过滤。"
                "当用户询问'有哪些作业''查看作业'某分区有什么作业'时调用。"
                "回答'现在有多少作业在运行/排队'必须以本工具返回为准："
                "逐条统计 job_state 字段（RUNNING/PENDING 等）。"
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
                "通过受控后端向当前项目提交作业。模型只提供命令正文和结构化资源，"
                "不要生成 shebang、#SBATCH、cd、日志路径或 Conda 激活命令；"
                "这些内容由后端根据当前项目锁定生成。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "作业命令正文，例如 srun python -u train.py；不得包含 #SBATCH。",
                    },
                    "partition": {
                        "type": "string",
                        "description": "目标分区名，如 P107-RTX5090",
                    },
                    "account": {
                        "type": "string",
                        "description": "计费账户，如 competition",
                    },
                    "qos": {
                        "type": "string",
                        "description": "QoS，如 qos_p107-rtx5090",
                    },
                    "name": {
                        "type": "string",
                        "description": "作业名称，用于在队列中标识",
                    },
                    "nodes": {
                        "type": "integer",
                        "description": "申请节点数，默认 1",
                    },
                    "cpus_per_task": {
                        "type": "integer",
                        "description": "每个任务的 CPU 核数，默认 1",
                    },
                    "gpus_per_node": {
                        "type": "integer",
                        "description": "每个节点的 GPU 数；需要 GPU 时必须明确大于 0",
                    },
                    "memory_mb": {
                        "type": "integer",
                        "description": "每个节点内存（MB），默认 16384",
                    },
                    "time_limit": {
                        "type": "integer",
                        "description": "运行时间上限（分钟），默认 60",
                    },
                },
                "required": ["command", "partition", "account", "qos"],
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
                "查看集群控制器诊断统计（slurmctld 累计口径，自控制器启动以来的计数）。"
                "注意：其中 statistics 里的 jobs_pending/jobs_running 是累计统计，"
                "不是当前队列里的实时数量；回答当前作业数量请改用 list_jobs。"
                "本工具适合回答'控制器状态''调度器运行情况'类问题。"
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
    {
        "type": "function",
        "function": {
            "name": "list_project_files",
            "description": (
                "列出当前项目目录的文件结构（目录树）。"
                "当用户询问'项目里有哪些文件''目录结构''有哪些脚本'时调用。"
                "读取具体文件内容前，通常先调用本工具了解有哪些文件。"
                "只列出当前选中项目目录内的文件，不包含 .slurm-agent 等内部目录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subdir": {
                        "type": "string",
                        "description": (
                            "可选，只列出项目内某个子目录。必须是相对项目根目录的路径，"
                            "不要以 / 开头，不要用绝对路径，如 src 或 data。不传则列出整个项目。"
                        ),
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "目录树最大深度，默认 3。目录很深时可减小避免输出过长。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_project_file",
            "description": (
                "读取当前项目目录里某个文件的内容，可按行号区间读取。"
                "当用户说'看看 train.py''读一下 main.py 第 300-340 行'"
                "'这个脚本写了什么'时调用。"
                "返回内容带行号，方便引用。只能读取项目目录内的文本文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "文件路径，相对项目根目录（不要以 / 开头，不要用绝对路径），"
                            "如 train.py 或 src/model.py。路径层级以 list_project_files"
                            "的输出为准。"
                        ),
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（1-based，含）。不传则从第 1 行开始。",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（1-based，含）。不传则读到文件末尾。",
                    },
                },
                "required": ["path"],
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
    "list_project_files": "列出项目目录的文件结构",
    "read_project_file": "按行读取项目里的文件内容",
}


# =========================================================================
# 工具执行调度
# =========================================================================


class ToolExecutor:
    """工具执行器：根据工具名和参数调用对应的 slurm_client 函数。"""

    def __init__(
        self,
        client: Optional[SlurmClient] = None,
        submit_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        submission_context: Optional[Dict[str, Any]] = None,
    ):
        self.client = client or SlurmClient()
        self.submit_handler = submit_handler
        self.submission_context = dict(submission_context or {})

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
                if self.submit_handler is None:
                    return (
                        "提交被拒绝：当前会话没有绑定项目级受控提交后端。"
                        "请在 Web 中选择项目后再提交作业。"
                    )
                draft = dict(self.submission_context)
                draft.update({
                    "command": arguments.get("command", ""),
                    "partition": arguments.get("partition", ""),
                    "account": arguments.get("account", ""),
                    "qos": arguments.get("qos", ""),
                    "job_name": arguments.get("name", "api-job"),
                    "nodes": arguments.get("nodes", 1),
                    "cpus_per_task": arguments.get("cpus_per_task", 1),
                    "gpus_per_node": arguments.get("gpus_per_node", 0),
                    "memory_mb": arguments.get("memory_mb", 16384),
                    "time_limit": arguments.get("time_limit", 60),
                    "source": "agent",
                })
                result = self.submit_handler(draft)
                job_id = result.get("job_id")
                verification = result.get("resource_verification") or {}
                verification_text = verification.get("message") or "资源字段等待 Slurm 确认"
                return (
                    f"作业已提交，job_id={job_id}。{verification_text}。"
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
                return self._format_diag_result(result)

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

            elif tool_name == "list_project_files":
                return self._list_project_files(arguments)

            elif tool_name == "read_project_file":
                return self._read_project_file(arguments)

            else:
                return f"未知工具: {tool_name}"

        except Exception as e:
            logger.error("工具执行失败: %s", e)
            return f"工具 {tool_name} 执行出错: {e}"

    # ---- 项目文件读取 ----

    def _project_dir(self) -> "Path":
        """解析当前会话绑定的项目目录；未绑定项目时抛错。"""
        from pathlib import Path
        from core.file_transfer import project_workspace
        project_name = self.submission_context.get("project_name", "")
        if not project_name:
            raise RuntimeError("当前会话未绑定项目，无法读取项目文件。请先在左侧选择作业目录。")
        _, project_dir, _ = project_workspace(project_name)
        return project_dir

    def _resolve_project_path(self, rel_path: str) -> "Path":
        """把路径安全解析到项目目录内，拒绝路径穿越。

        对 LLM 常见写法容错：前导 / 视为相对项目根；完整绝对路径若
        落在项目目录内也直接接受（LLM 常复制 list 输出头部的绝对路径）。
        .. 穿越与越出项目目录一律拒绝。
        """
        from pathlib import Path, PurePosixPath
        project_dir = self._project_dir()

        cleaned = (rel_path or "").replace("\\", "/").strip()
        if not cleaned:
            raise RuntimeError("路径为空")

        original = Path(cleaned)
        if original.is_absolute():
            resolved = original.resolve()
            try:
                resolved.relative_to(project_dir)
            except ValueError:
                pass  # 不在项目内则退回相对解析（前导 / 视为项目根）
            else:
                return resolved

        rel = PurePosixPath(cleaned.lstrip("/"))
        # 过滤空段（双斜杠产生），拒绝 . 和 ..
        parts = [p for p in rel.parts if p]
        if any(p in (".", "..") for p in parts):
            raise RuntimeError(f"路径不允许包含 . 或 ..: {rel_path}")
        if not parts:
            raise RuntimeError("路径为空")

        target = (project_dir / Path(*parts)).resolve()
        try:
            target.relative_to(project_dir)
        except ValueError:
            raise RuntimeError(f"路径不在项目目录内: {rel_path}（项目目录: {project_dir}）")
        return target

    def _top_level_hint(self) -> str:
        """项目根目录顶层条目摘要，附在“不存在”类错误里帮 LLM 自行纠错
        （如 zip 解压产生的嵌套同名目录：路径需以 MQAB4AF2/ 开头）。"""
        from pathlib import Path
        try:
            project_dir = self._project_dir()
            entries = sorted(
                p.name + ("/" if p.is_dir() else "")
                for p in project_dir.iterdir()
                if not p.name.startswith(".")
            )
            if not entries:
                return ""
            return f"项目根目录顶层条目: {', '.join(entries[:12])}"
        except Exception:
            return ""

    def _list_project_files(self, arguments: Dict[str, Any]) -> str:
        from pathlib import Path
        project_dir = self._project_dir()
        subdir = (arguments.get("subdir") or "").strip().strip("/")
        max_depth = int(arguments.get("max_depth") or 3)
        max_depth = max(1, min(max_depth, 8))

        base = project_dir
        if subdir:
            base = self._resolve_project_path(subdir)
            if not base.is_dir():
                hint = self._top_level_hint()
                return f"子目录不存在: {subdir}" + (f"（{hint}）" if hint else "")

        # 内部目录/文件，不展示
        SKIP_DIRS = {".slurm-agent", ".git", "__pycache__", ".venv", "venv"}
        SKIP_FILES = {".DS_Store"}
        MAX_ENTRIES = 300

        lines: list[str] = []
        count = 0

        def walk(directory: Path, prefix: str, depth: int) -> None:
            nonlocal count
            if depth > max_depth or count >= MAX_ENTRIES:
                return
            try:
                entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            for entry in entries:
                if count >= MAX_ENTRIES:
                    return
                if entry.name in SKIP_DIRS or entry.name in SKIP_FILES:
                    continue
                if entry.name.startswith(".") and entry.name not in {".gitignore", ".env"}:
                    continue
                rel = entry.relative_to(project_dir)
                if entry.is_dir():
                    lines.append(f"{prefix}{entry.name}/")
                    count += 1
                    walk(entry, prefix + "  ", depth + 1)
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"{prefix}{entry.name}  ({size} B)")
                    count += 1

        walk(base, "", 1)
        if not lines:
            return f"项目目录 {project_dir} 下没有可列出的文件。"
        header = f"## 项目文件结构（根目录: {project_dir}）\n\n以下路径均相对于项目根目录。\n\n"
        if count >= MAX_ENTRIES:
            header += f"（文件过多，仅列出前 {MAX_ENTRIES} 项）\n\n"
        return header + "\n".join(lines)

    def _read_project_file(self, arguments: Dict[str, Any]) -> str:
        rel_path = (arguments.get("path") or "").strip()
        if not rel_path:
            return "（read_project_file 需要提供 path 参数）"
        target = self._resolve_project_path(rel_path)
        if not target.is_file():
            hint = self._top_level_hint()
            return f"文件不存在: {rel_path}" + (f"（{hint}）" if hint else "")
        # 已知二进制扩展名直接拒绝（压缩/图片/库等，内容检测不可靠）
        BINARY_SUFFIXES = {
            ".pak", ".dylib", ".so", ".a", ".o", ".bin", ".exe", ".dll",
            ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".webp",
            ".zip", ".gz", ".tar", ".xz", ".bz2", ".7z", ".rar",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
            ".npy", ".npz", ".pkl", ".pickle", ".pt", ".pth", ".ckpt",
            ".h5", ".hdf5", ".parquet", ".feather", ".onnx",
        }
        if target.suffix.lower() in BINARY_SUFFIXES:
            return f"该文件是二进制文件（{target.suffix}），无法按文本读取: {rel_path}"
        # 内容兜底：控制字节比例过高也判为二进制
        try:
            head = target.read_bytes()[:8192]
        except OSError as e:
            return f"读取文件失败: {e}"
        if head:
            control_bytes = sum(1 for b in head if b < 9 or (13 < b < 32))
            if control_bytes / len(head) > 0.08:
                return f"该文件是二进制文件，无法按文本读取: {rel_path}"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"读取文件失败: {e}"

        lines = text.splitlines()
        total = len(lines)
        start = int(arguments.get("start_line") or 1)
        end = int(arguments.get("end_line") or total)
        start = max(1, min(start, total))
        end = max(start, min(end, total))

        # 单次最多读 400 行，防止撑爆上下文
        MAX_LINES = 400
        if end - start + 1 > MAX_LINES:
            end = start + MAX_LINES - 1

        selected = lines[start - 1:end]
        numbered = "\n".join(
            f"{i + start:>5} | {line}" for i, line in enumerate(selected)
        )
        header = (
            f"## {rel_path}\n"
            f"总行数: {total}，显示第 {start}-{end} 行\n\n"
        )
        if start > 1:
            header += f"...（省略前 {start - 1} 行）...\n"
        body = header + numbered
        if end < total:
            body += f"\n...（省略后 {total - end} 行）..."
        return body

    # ---- 结果格式化 ----

    def _format_diag_result(self, result: Dict[str, Any]) -> str:
        """get_diag 结果补注：statistics 为控制器累计统计口径，附实时作业统计防混淆。"""
        try:
            jobs = self.client.list_jobs().get("jobs", [])
            counts: Dict[str, int] = {}
            for job in jobs:
                # job_state 可能是 "PENDING + REASON" 或 "PENDING+REQUEUE" 等组合，取主状态
                state = (job.get("job_state") or "UNKNOWN").split()[0].split("+")[0]
                counts[state] = counts.get(state, 0) + 1
            enriched = dict(result)
            enriched["_note"] = (
                "statistics 字段是 slurmctld 控制器自启动以来的累计统计（sdiag 口径），"
                "不代表当前队列里的作业数量；回答当前运行/排队作业数"
                "以下面的 current_jobs_summary 实时统计为准。"
            )
            enriched["current_jobs_summary"] = counts
            return self._format_json(enriched)
        except Exception as e:
            logger.warning("补充实时作业统计失败，退回原始 get_diag 结果: %s", e)
            return self._format_json(result)

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
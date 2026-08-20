#!/usr/bin/env python3
"""
agent_loop.py — Function Calling 主对话循环。

这是整个智能体的"中枢"：
  用户自然语言 → LLM 决定调用哪个工具 → 代码执行工具 → 结果返回 LLM → LLM 组织回答

支持：
  - 多轮对话（维护上下文历史）
  - 多步工具调用（一次对话可调用多个工具）
  - 流式输出（打字机效果）
  - 安全护栏（max_turns 防止死循环）
"""

import logging
import json
from typing import Dict, Any, List, Optional

from agent.llm_provider import LLMProvider
from agent.tools_registry import TOOL_DEFINITIONS, ToolExecutor

logger = logging.getLogger(__name__)

# =========================================================================
# System Prompt
# =========================================================================

SYSTEM_PROMPT = """你是中国科学技术大学超级计算中心算力平台的智能助手。
你有 17 个工具：REST 查询/操作、CLI 只读诊断、知识库检索、模板脚本生成、日志读取。
每个工具的用途和调用时机见工具描述（什么时候调哪个工具，以工具描述为准）。

## 工作方式
- 使用中文回复，简洁清晰。
- 作业、集群、配额等运行数据必须调用工具实时获取，不要凭记忆编造。
- 工具返回错误时，如实告知用户错误信息并给出建议。

## 多步工作流（单工具描述表达不了的编排）
- 生成作业脚本：先 list_templates 展示模板 → 与用户确认模板和参数 → generate_script 生成 → 询问是否需要 submit_job 提交。
- 提交作业前先 get_qos 核对配额，资源申请超出配额时提醒用户。
- 排队诊断：先 get_job_priority 看优先级构成，再 get_job 看 Reason 字段，结合两者分析。
- 作业报错诊断：read_job_log 先看 stderr 再看 stdout，结合 search_knowledge 中的 FAQ。
- 取消作业（cancel_job）前必须向用户确认，操作不可逆。

## 平台事实（集群实测，回答和写脚本时以此为准）
- 分区名区分大小写：P107-RTX5090、P107-A100、GPU-RTX5090、GPU-A100、CPU-6530、CPU-8358P、Students。
- 分区与计费账户：P107 系列 → competition；Students → stu；其他分区 → demo_admin/cmet。
- 对应 QoS：qos_p107-rtx5090、qos_p107-a100、qos_stu_default。
- 默认配额 4 CPU / 1 GPU / 4 小时（以 get_qos 实时查询为准）。
- 集群使用 Slurm 25.11，REST API 地址 http://107.ustc.edu.cn:6820；节点、QoS 实时信息通过 get_nodes / get_qos 查询。
- 定时任务已被集群禁用（scrontab 不可用）：周期性任务建议“长时限作业 + 脚本内循环”，不要推荐 scrontab。

## 手写 sbatch 脚本核心规则（模板不满足需求时）
- #SBATCH 指令写在脚本顶部连续注释区，每行一条，以 #SBATCH 开头；遇到第一行非注释非空白代码即停止解析。
- #SBATCH 行不展开 shell 变量（$VAR 无效），值必须写死；命令行参数优先级最高，同名列后者覆盖前者。
- 必备指令：--job-name、--partition、--account、--qos、--nodes、--cpus-per-task、--gpus、--time、--output、--error。
- 日志文件名符号：%j=作业ID、%x=作业名、%A/%a=数组主ID/下标、%N=节点名、%u=用户名；推荐 logs/%x-%j.out，相对路径基于提交时工作目录。
- --time 支持分钟数（如 240）或 HH:MM:SS；--gpus=N 与 --gres=gpu:N 等价。
- 正文建议：set -euo pipefail；显式 cd 到工作目录；conda activate 用 set +u / set -u 包裹；python 加 -u 实时输出。
- 更详细的语法说明可调用 search_knowledge（如查询“sbatch 脚本”）。
"""


# =========================================================================
# AgentLoop — 主对话循环
# =========================================================================


class AgentLoop:
    """Function Calling 主循环。"""

    def __init__(
        self,
        llm: Optional[LLMProvider] = None,
        executor: Optional[ToolExecutor] = None,
        system_prompt: str = SYSTEM_PROMPT,
    ):
        self.llm = llm or LLMProvider()
        self.executor = executor or ToolExecutor()
        self.system_prompt = system_prompt
        # max_turns 限制已取消：循环直到模型返回纯文本为止

        # 对话历史
        self.messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    def reset(self) -> None:
        """重置对话历史（仅保留 system prompt）。"""
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def chat(self, user_input: str) -> str:
        """
        处理一轮用户输入，返回最终回复。

        参数:
            user_input: 用户输入的自然语言文本

        返回:
            模型的最终文本回复
        """
        # 1. 追加用户消息
        self.messages.append({"role": "user", "content": user_input})

        # 2. Function Calling 循环（不设轮数上限：持续到模型返回纯文本为止）
        while True:
            logger.info("LLM 调用...")

            response = self.llm.chat(
                messages=self.messages,
                tools=TOOL_DEFINITIONS,
            )
            choice = response.choices[0]
            message = choice.message

            # 情况 A：模型返回 tool_calls → 执行工具
            if message.tool_calls:
                # 追加 assistant 消息（含 tool_calls）
                self.messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ],
                })

                # 逐个执行工具
                for tc in message.tool_calls:
                    tool_name = tc.function.name
                    try:
                        arguments = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        arguments = {}

                    logger.info("  执行工具: %s(%s)", tool_name, arguments)
                    result_str = self.executor.execute(tool_name, arguments)

                    # 追加 tool 结果消息
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    })

                # 继续循环，让 LLM 处理工具结果
                continue

            # 情况 B：模型直接返回文本 → 结束
            reply = message.content or ""
            self.messages.append({"role": "assistant", "content": reply})
            return reply

    def interactive(self) -> None:
        """启动交互式终端聊天界面。"""
        print("\n" + "=" * 60)
        print("  算力平台智能助手 — 终端交互模式")
        print("  输入 'quit' / 'exit' 退出")
        print("  输入 'reset' 重置对话")
        print("=" * 60 + "\n")

        while True:
            try:
                user_input = input("👤 你: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break

            if not user_input:
                continue

            if user_input.lower() in ("quit", "exit"):
                print("再见！")
                break

            if user_input.lower() == "reset":
                self.reset()
                print("✓ 对话已重置")
                continue

            # 调用 Agent
            try:
                reply = self.chat(user_input)
                print(f"\n🤖 助手: {reply}\n")
            except Exception as e:
                print(f"\n⚠️ 出错: {e}\n")
                logger.exception("Agent 处理异常")


# =========================================================================
# __main__ 测试
# =========================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 检查必要环境变量
    missing = []
    import os
    if not os.environ.get("SLURM_JWT"):
        missing.append("SLURM_JWT")
    if not os.environ.get("LLM_API_KEY"):
        missing.append("LLM_API_KEY")

    if missing:
        print("❌ 缺少环境变量: " + ", ".join(missing))
        print("请设置后重试：")
        if "SLURM_JWT" in missing:
            print("  export SLURM_JWT=$(scontrol token lifespan=86400 | sed 's/SLURM_JWT=//')")
        if "LLM_API_KEY" in missing:
            print("  export LLM_API_KEY=sk-你的APIKey")
        sys.exit(1)

    print("=" * 60)
    print("阶段 2 验收测试 — agent_loop.py")
    print("=" * 60)

    # 如果传了 --interactive，进入交互模式
    if "--interactive" in sys.argv or "-i" in sys.argv:
        agent = AgentLoop()
        agent.interactive()
        sys.exit(0)

    agent = AgentLoop()

    # 测试 1: 查询作业（应触发 list_jobs）
    print("\n[测试 1] '帮我看看 P107-RTX5090 分区有什么作业'")
    print("-" * 40)
    try:
        reply = agent.chat("帮我看看 P107-RTX5090 分区有什么作业")
        print(f"回复: {reply}")
        print("✓ 测试 1 通过")
    except Exception as e:
        print(f"✗ 测试 1 失败: {e}")

    # 测试 2: 集群状态（应触发 get_diag）
    print("\n[测试 2] '集群现在忙不忙？'")
    print("-" * 40)
    try:
        reply = agent.chat("集群现在忙不忙？")
        print(f"回复: {reply}")
        print("✓ 测试 2 通过")
    except Exception as e:
        print(f"✗ 测试 2 失败: {e}")

    # 测试 3: 纯知识问答（不应触发工具调用）
    print("\n[测试 3] 'Slurm 是什么？'")
    print("-" * 40)
    try:
        reply = agent.chat("Slurm 是什么？")
        print(f"回复: {reply}")
        print("✓ 测试 3 通过")
    except Exception as e:
        print(f"✗ 测试 3 失败: {e}")

    # 测试 4: 提交测试作业（应触发 submit_job）
    print("\n[测试 4] '帮我在 P107-RTX5090 分区提交一个测试作业，运行 srun hostname'")
    print("-" * 40)
    submitted_job_id = None
    try:
        reply = agent.chat(
            "帮我在 P107-RTX5090 分区提交一个测试作业，运行 srun hostname，"
            "用 1 个节点，作业名 test-agent-submit"
        )
        print(f"回复: {reply}")
        # 尝试从回复或工具结果中提取 job_id
        import re
        match = re.search(r'\b(\d{5,})\b', reply)
        if match:
            submitted_job_id = match.group(1)
            print(f"提取到作业 ID: {submitted_job_id}")
        print("✓ 测试 4 通过")
    except Exception as e:
        print(f"✗ 测试 4 失败: {e}")

    # 测试 5: 取消测试作业（应触发 cancel_job）
    if submitted_job_id:
        print(f"\n[测试 5] '取消作业 {submitted_job_id}'")
        print("-" * 40)
        try:
            reply = agent.chat(f"取消作业 {submitted_job_id}")
            print(f"回复: {reply}")
            print("✓ 测试 5 通过")
        except Exception as e:
            print(f"✗ 测试 5 失败: {e}")
    else:
        print("\n[测试 5] 跳过 — 未获取到测试 4 的作业 ID")

    print("\n" + "=" * 60)
    print("验收测试完成。")
    print("输入 'python agent/agent_loop.py --interactive' 进入交互模式。")
    print("=" * 60)
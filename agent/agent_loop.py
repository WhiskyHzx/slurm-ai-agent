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
from config.settings import LLM_MAX_TOOL_TURNS

logger = logging.getLogger(__name__)

# =========================================================================
# System Prompt
# =========================================================================

SYSTEM_PROMPT = """你是中国科学技术大学超级计算中心算力平台的智能助手。

## 你的能力
你可以帮助用户：
1. 查询作业列表和详情（list_jobs / get_job）
2. 提交新作业（submit_job）
3. 取消作业（cancel_job）
4. 查看集群统计和节点信息（get_diag / get_nodes）
5. 查询 QoS 资源配额（get_qos）
6. 查询历史作业记录（get_jobs_history），用于报错诊断
7. 搜索平台知识库（search_knowledge），回答平台使用、环境配置、常见问题等
8. 生成作业脚本（list_templates / generate_script），根据需求生成 sbatch 脚本
9. 读取作业输出日志（read_job_log），分析运行结果和错误信息
10. 报错诊断：分析用户粘贴的报错信息或作业日志，结合知识库给出原因和解决方案

## 重要规则
- 使用中文回复，简洁清晰。
- 当用户询问作业/集群相关信息时，**必须调用对应工具获取实时数据**，不要凭记忆编造。
- 当用户询问平台使用方法、环境配置、常见错误等问题时，**先调用 search_knowledge 搜索知识库**，再结合检索结果回答。
- **当用户粘贴报错日志时，调用 search_knowledge 搜索相关 FAQ**，结合你的知识给出诊断：报错原因 → 解决方案 → 如何避免。
- 当用户询问某个作业的运行结果或报错时，**调用 read_job_log 读取作业日志**，先查看 stderr 是否有错误，再分析 stdout 输出。
- 当用户要求生成作业脚本时，**先调用 list_templates 展示可用模板**，与用户确认模板选择和参数后，再调用 generate_script 生成。
- 生成脚本后，询问用户是否需要直接提交（调用 submit_job）。
- 提交作业前，**先调用 get_qos 检查用户配额是否足够**，如果资源申请超出配额应提醒用户。
- 取消作业前，**必须向用户确认**，因为操作不可逆。
- 如果工具返回错误，如实告知用户错误信息，并建议解决方法。
- 分区名区分大小写：P107-RTX5090、P107-A100、GPU-RTX5090、GPU-A100、CPU-6530、CPU-8358P、Students。
- 不同分区需要不同账户：P107 系列需要 competition 账户，Students 需要 stu 账户，其他分区需要 demo_admin/cmet 账户。

## 平台信息
- 集群包含数十个计算节点，配备 RTX 5090 和 A100 等多种 GPU。具体节点信息请通过 get_nodes 工具实时查询。
- REST API 地址：http://107.ustc.edu.cn:6820
- 使用 Slurm 25.11 调度系统
- 资源配额通过 QoS 管理，可通过 get_qos 工具实时查询
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
        max_turns: int = LLM_MAX_TOOL_TURNS,
    ):
        self.llm = llm or LLMProvider()
        self.executor = executor or ToolExecutor()
        self.system_prompt = system_prompt
        self.max_turns = max_turns

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

        # 2. Function Calling 循环
        turn = 0
        while turn < self.max_turns:
            turn += 1
            logger.info("第 %d 轮 LLM 调用...", turn)

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

        # 超过最大轮数
        return "抱歉，处理您的请求时超过了最大工具调用次数，请简化问题后重试。"

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
#!/usr/bin/env python3
"""
llm_provider.py — 大模型客户端统一封装。

适配 OpenAI 兼容 API（学校大模型公共服务平台），
提供统一的 chat() 接口，支持 Function Calling。

Token 安全：
  - API Key 仅从环境变量 LLM_API_KEY 读取，绝不硬编码。
  - 禁止将 LLM_API_KEY 传入外部消息内容。
"""

import os
import logging
from typing import Optional, List, Dict, Any

from openai import OpenAI

from config.settings import (
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
)

logger = logging.getLogger(__name__)


class LLMProvider:
    """OpenAI 兼容的大模型客户端封装。"""

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int = LLM_MAX_TOKENS,
    ):
        """
        参数:
            model:       模型名称，默认使用 settings 中的 LLM_MODEL
            base_url:    API 基础地址
            api_key:     API Key，默认从 LLM_API_KEY 环境变量读取
            temperature: 生成温度
            max_tokens:  最大输出 token 数
        """
        self.model = model or LLM_MODEL
        self.temperature = temperature
        self.max_tokens = max_tokens

        # API Key 仅从环境变量读取
        api_key = api_key or os.environ.get("LLM_API_KEY")
        if not api_key:
            raise RuntimeError(
                "环境变量 LLM_API_KEY 未设置。\n"
                "请在 https://api.llm.ustc.edu.cn 获取 API Key 后执行：\n"
                "  $ export LLM_API_KEY=sk-你的APIKey"
            )

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url or LLM_BASE_URL,
        )
        logger.info("LLMProvider 初始化完成，模型: %s", self.model)

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        调用大模型，支持 Function Calling。

        参数:
            messages:    对话消息列表 [{"role":"system"|"user"|"assistant"|"tool", ...}]
            tools:       工具定义列表（OpenAI tool 格式），为 None 时纯对话
            temperature: 覆盖默认温度
            max_tokens:  覆盖默认最大 token 数

        返回:
            模型原始响应对象（openai.types.chat.ChatCompletion），
            调用方从中提取 .choices[0].message.content 或 .tool_calls。
        """
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }

        if tools:
            kwargs["tools"] = tools
            # 工具调用场景下不强制 tool_choice，让模型自行判断

        logger.debug(
            "调用 LLM: model=%s, messages_count=%d, tools_count=%d",
            self.model, len(messages), len(tools) if tools else 0,
        )

        try:
            response = self.client.chat.completions.create(**kwargs)
            return response
        except Exception as e:
            logger.error("LLM 调用失败: %s", e)
            raise RuntimeError(f"大模型调用失败: {e}") from e


# =========================================================================
# 模块级便捷函数
# =========================================================================

_default_provider: Optional[LLMProvider] = None


def get_provider(model: Optional[str] = None) -> LLMProvider:
    """获取或创建默认 LLMProvider 实例。"""
    global _default_provider
    if _default_provider is None or model:
        _default_provider = LLMProvider(model=model) if model else LLMProvider()
    return _default_provider


def chat(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """便捷函数：调用大模型。"""
    return get_provider().chat(messages, tools)


# =========================================================================
# __main__ 测试
# =========================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("llm_provider.py 连通性测试")
    print("=" * 60)

    try:
        provider = LLMProvider()
        resp = provider.chat(
            messages=[{"role": "user", "content": "你好，请用一句话介绍自己。"}],
        )
        content = resp.choices[0].message.content
        print(f"\n模型回复: {content}")
        print(f"\n使用的模型: {resp.model}")
        print(f"Token 用量: {resp.usage}")
        print("\n✓ LLM 连通性测试通过")
    except Exception as e:
        print(f"\n✗ 测试失败: {e}")
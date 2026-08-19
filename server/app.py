#!/usr/bin/env python3
"""
server/app.py — FastAPI 后端入口。

将 AgentLoop 包装成 HTTP API，提供：
  - POST /chat     发送消息，SSE 流式返回（含工具调用过程）
  - POST /reset    重置对话
  - GET  /         返回前端页面

启动方式：
  uvicorn server.app:app --host 0.0.0.0 --port 8080
"""

import json
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.agent_loop import AgentLoop, SYSTEM_PROMPT
from agent.llm_provider import LLMProvider
from agent.tools_registry import TOOL_DEFINITIONS, ToolExecutor
from core.local_env import find_existing_socks_proxy, load_dotenv, set_dotenv_value
from core.token_manager import get_token_status, refresh_token_via_ssh, update_token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
app = FastAPI(title="算力平台智能助手", version="1.0")

# ---------------------------------------------------------------------------
# 全局 Agent 实例（每个请求共享同一个对话历史）
# ---------------------------------------------------------------------------
agent: Optional[AgentLoop] = None


def get_agent() -> AgentLoop:
    """获取或懒初始化 AgentLoop 实例。"""
    global agent
    if agent is None:
        agent = AgentLoop()
    return agent


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str


class ResetResponse(BaseModel):
    status: str


class TokenUpdateRequest(BaseModel):
    token: str


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------


@app.post("/chat")
async def chat(req: ChatRequest):
    """
    发送消息给智能体，SSE 流式返回。

    流式事件类型：
      - tool_start: 开始执行工具 {"tool": "read_job_log", "args": {...}}
      - tool_end:   工具执行完成 {"tool": "read_job_log", "result": "..."}
      - text:       LLM 文本回复片段
      - done:       对话完成
      - error:      出错
    """
    user_message = req.message.strip()
    if not user_message:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)

    async def event_stream():
        try:
            ag = get_agent()
            ag.messages.append({"role": "user", "content": user_message})

            turn = 0
            while turn < ag.max_turns:
                turn += 1
                logger.info("第 %d 轮 LLM 调用...", turn)

                # 调用 LLM
                try:
                    response = ag.llm.chat(
                        messages=ag.messages,
                        tools=TOOL_DEFINITIONS,
                    )
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'content': f'LLM调用失败: {e}'}, ensure_ascii=False)}\n\n"
                    return

                choice = response.choices[0]
                message = choice.message

                # 情况 A：模型返回 tool_calls
                if message.tool_calls:
                    # 追加 assistant 消息（含 tool_calls）
                    tool_calls_data = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ]
                    ag.messages.append({
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": tool_calls_data,
                    })

                    # 逐个执行工具
                    for tc in message.tool_calls:
                        tool_name = tc.function.name
                        try:
                            arguments = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            arguments = {}

                        # 发送 tool_start 事件
                        yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name, 'args': arguments}, ensure_ascii=False)}\n\n"

                        try:
                            result_str = ag.executor.execute(tool_name, arguments)
                        except Exception as e:
                            result_str = f"工具执行出错: {e}"

                        # 发送 tool_end 事件
                        yield f"data: {json.dumps({'type': 'tool_end', 'tool': tool_name, 'result': result_str[:500]}, ensure_ascii=False)}\n\n"

                        # 追加 tool 结果消息
                        ag.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_str,
                        })

                    continue  # 继续循环

                # 情况 B：模型直接返回文本 → 流式输出
                reply = message.content or ""
                ag.messages.append({"role": "assistant", "content": reply})

                # 按句子/段落切分，模拟流式输出
                # 简单按字符块发送
                chunk_size = 20
                for i in range(0, len(reply), chunk_size):
                    chunk = reply[i:i + chunk_size]
                    yield f"data: {json.dumps({'type': 'text', 'content': chunk}, ensure_ascii=False)}\n\n"

                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            # 超过最大轮数
            yield f"data: {json.dumps({'type': 'error', 'content': '超过最大工具调用轮数，请简化问题后重试。'}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.exception("处理请求异常")
            yield f"data: {json.dumps({'type': 'error', 'content': f'服务异常: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/reset", response_model=ResetResponse)
async def reset():
    """重置对话历史。"""
    global agent
    if agent:
        agent.reset()
    else:
        agent = AgentLoop()
    return ResetResponse(status="ok")


@app.get("/api/token/status")
async def token_status():
    """Return local SLURM_JWT status without exposing the token."""
    status = get_token_status()
    return {
        "present": status.present,
        "preview": status.preview,
        "expires_at": status.expires_at,
        "seconds_remaining": status.seconds_remaining,
        "expired": status.expired,
        "refresh_command": status.refresh_command,
        "ssh_host": status.ssh_host,
    }


@app.post("/api/token/update")
async def token_update(req: TokenUpdateRequest):
    """
    Update local SLURM_JWT from a user-provided token.

    Manual fallback for users who cannot run remote ssh commands from the local
    app process.
    """
    try:
        status = update_token(req.token)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    return {
        "status": "ok",
        "present": status.present,
        "preview": status.preview,
        "expires_at": status.expires_at,
        "seconds_remaining": status.seconds_remaining,
        "expired": status.expired,
    }


@app.post("/api/token/refresh")
async def token_refresh():
    """
    Refresh SLURM_JWT by running `scontrol token ...` over system ssh.

    This does not implement SSH login. It succeeds when the user's local ssh
    setup can already run non-interactive remote commands.
    """
    try:
        status = refresh_token_via_ssh()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    return {
        "status": "ok",
        "present": status.present,
        "preview": status.preview,
        "expires_at": status.expires_at,
        "seconds_remaining": status.seconds_remaining,
        "expired": status.expired,
        "ssh_host": status.ssh_host,
    }


@app.post("/api/proxy/refresh")
async def proxy_refresh():
    """
    Re-scan existing VS Code Remote-SSH SOCKS proxy and store it in .env.

    This does not start SSH. It only reuses a proxy from an already-connected
    VS Code Remote-SSH session or a user-started `ssh -D` SOCKS tunnel.
    """
    env_values = load_dotenv()
    proxy = find_existing_socks_proxy(env_values.get("SLURM_API_PROXY", ""))
    if not proxy:
        return JSONResponse(
            {
                "error": (
                    "未找到可用的 VS Code Remote-SSH SOCKS 端口。"
                    "请先在 VS Code 连接 107.ustc.edu.cn。"
                )
            },
            status_code=404,
        )

    changed = proxy != env_values.get("SLURM_API_PROXY", "")
    if changed:
        set_dotenv_value("SLURM_API_PROXY", proxy)

    return {"status": "ok", "proxy": proxy, "changed": changed}


# ---------------------------------------------------------------------------
# 前端页面
# ---------------------------------------------------------------------------


@app.get("/")
async def index():
    """返回聊天界面 HTML 页面。"""
    from pathlib import Path
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h2>前端页面未找到，请创建 server/static/index.html</h2>")


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "agent_ready": agent is not None}


# =========================================================================
# __main__ 直接启动
# =========================================================================

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    uvicorn.run(app, host="0.0.0.0", port=8080)

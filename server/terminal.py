#!/usr/bin/env python3
"""
terminal.py — Web 控制台直连终端（PTY + WebSocket）。

原理（三层结构）：
  浏览器 xterm.js（终端模拟器，渲染 ANSI 转义序列）
      │  WebSocket 双向字节流
      ▼
  FastAPI WebSocket 端点（本文件）
      │  os.write / os.read
      ▼
  PTY（伪终端，master/slave 对）→ bash 子进程

消息协议：
  - 二进制帧   = 键盘输入字节（浏览器 → PTY）
  - 文本帧     = JSON 控制消息（浏览器 → PTY）：
      {"type": "resize", "cols": 80, "rows": 24}
  - 服务器 → 浏览器：二进制帧 = PTY 输出字节

安全：
  - Origin 校验：WebSocket 不受浏览器 CORS 保护，必须手动校验，
    防止恶意网页发起跨站 WebSocket 劫持（CSWSH）。
  - project_name 复用 core.file_transfer.project_workspace() 清洗，防路径穿越。
  - 全局 PTY 并发上限，防止登录节点资源被耗尽。
  - 断连清理：先 SIGHUP（模拟关闭终端窗口），超时再 SIGKILL，避免僵尸 shell。
"""

import asyncio
import fcntl
import json
import logging
import os
import struct
import termios
import threading
import time
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from core.file_transfer import project_workspace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------
MAX_TERMINAL_SESSIONS = 4          # 全局 PTY 并发上限（登录节点资源保护）
HUP_GRACE_SECONDS = 5              # SIGHUP 后等待 shell 自行退出的秒数
READ_CHUNK = 4096                  # 每次 os.read 的字节数
DEFAULT_COLS = 80
DEFAULT_ROWS = 24

# 活跃会话计数（跨 WebSocket 连接共享）
_active_sessions = 0
_sessions_lock = threading.Lock()


def _acquire_session_slot() -> bool:
    """占用一个会话名额，超过上限返回 False。"""
    global _active_sessions
    with _sessions_lock:
        if _active_sessions >= MAX_TERMINAL_SESSIONS:
            return False
        _active_sessions += 1
        return True


def _release_session_slot() -> None:
    global _active_sessions
    with _sessions_lock:
        _active_sessions = max(0, _active_sessions - 1)


def _set_window_size(fd: int, cols: int, rows: int) -> None:
    """ioctl(TIOCSWINSZ) 设置 PTY 窗口尺寸，内核会给前台进程组发 SIGWINCH。"""
    cols = max(20, min(int(cols), 500))
    rows = max(5, min(int(rows), 200))
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _resolve_cwd(project_name: Optional[str]) -> str:
    """
    解析终端初始工作目录。

    - 指定 project_name 时复用 project_workspace() 清洗（防路径穿越），
      目录存在则落在项目目录，否则回退 $HOME。
    - 未指定时落在 $HOME。
    """
    home = os.path.expanduser("~")
    if not project_name:
        return home
    try:
        _, project_dir, _ = project_workspace(project_name)
        if project_dir.is_dir():
            return str(project_dir)
    except Exception:
        pass
    return home


def _check_origin(websocket: WebSocket) -> bool:
    """
    校验 WebSocket Origin 头。

    同源请求（页面与 WS 同 host:port）Origin 与 Host 一致；
    非浏览器客户端可能不带 Origin，也放行（与项目现有 API 信任模型一致：
    服务只经 SSH 隧道/UDS 暴露，不直接暴露公网）。
    """
    origin = websocket.headers.get("origin", "")
    if not origin:
        return True
    host = websocket.headers.get("host", "")
    try:
        from urllib.parse import urlparse
        origin_host = urlparse(origin).netloc
        return (not host) or (origin_host == host)
    except Exception:
        return False


async def terminal_websocket(
    websocket: WebSocket,
    project_name: Optional[str] = None,
) -> None:
    """
    WebSocket 端点主逻辑：桥接浏览器 xterm.js 与 PTY 中的 bash。

    读方向（PTY → 浏览器）：后台线程阻塞 os.read(master_fd)，
      通过 loop.run_in_executor / call_soon_threadsafe 推回 WebSocket。
    写方向（浏览器 → PTY）：async 接收消息，二进制帧直接 os.write。
    """
    if not _check_origin(websocket):
        await websocket.close(code=4003, reason="Origin 不允许")
        return

    if not _acquire_session_slot():
        await websocket.close(code=4029, reason="终端会话数已达上限")
        return

    import ptyprocess

    cwd = _resolve_cwd(project_name)
    proc = None
    loop = asyncio.get_running_loop()
    reader_future = None

    try:
        await websocket.accept()

        # 启动 PTY 中的登录 shell
        # 设置 TERM=xterm-256color，让 bash 的彩色提示符（PS1）正常染色，
        # 否则 TERM=dumb 时 .bashrc 会退化为无颜色提示符。
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        proc = ptyprocess.PtyProcess.spawn(
            ["bash", "--login"],
            cwd=cwd,
            env=env,
            dimensions=(DEFAULT_ROWS, DEFAULT_COLS),
        )
        fd = proc.fd

        # 发送初始目录信息（前端可显示提示）
        await websocket.send_text(json.dumps({
            "type": "ready",
            "cwd": cwd,
            "pid": proc.pid,
        }))

        # -------------------------------------------------------------
        # 读线程：PTY → 浏览器
        # os.read 是阻塞调用，放线程池里跑，避免卡住事件循环
        # -------------------------------------------------------------
        def _pty_reader() -> None:
            while proc.isalive():
                try:
                    data = os.read(fd, READ_CHUNK)
                except OSError:
                    break
                if not data:
                    break
                asyncio.run_coroutine_threadsafe(
                    _safe_send_bytes(websocket, data), loop
                ).result(timeout=5)
            # PTY 结束：通知前端
            asyncio.run_coroutine_threadsafe(
                _safe_send_text(websocket, json.dumps({"type": "exit"})), loop
            )

        async def _safe_send_bytes(ws: WebSocket, data: bytes) -> None:
            try:
                await ws.send_bytes(data)
            except Exception:
                pass

        async def _safe_send_text(ws: WebSocket, text: str) -> None:
            try:
                await ws.send_text(text)
            except Exception:
                pass

        reader_future = loop.run_in_executor(None, _pty_reader)

        # -------------------------------------------------------------
        # 写方向：浏览器 → PTY（主协程循环）
        # -------------------------------------------------------------
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if "bytes" in message and message["bytes"] is not None:
                # 键盘输入：直接写入 PTY master
                if proc.isalive():
                    os.write(fd, message["bytes"])
            elif "text" in message and message["text"] is not None:
                # 控制消息（JSON）
                try:
                    ctrl = json.loads(message["text"])
                except (ValueError, TypeError):
                    continue
                ctype = ctrl.get("type")
                if ctype == "resize":
                    try:
                        _set_window_size(fd, ctrl.get("cols", DEFAULT_COLS),
                                         ctrl.get("rows", DEFAULT_ROWS))
                    except (OSError, ValueError, TypeError):
                        pass
                elif ctype == "ping":
                    await _safe_send_text(websocket, json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("终端 WebSocket 异常")
    finally:
        # -------------------------------------------------------------
        # 清理：先 SIGHUP（模拟关闭终端窗口），超时 SIGKILL
        # ptyprocess 无 signal() 方法：用 os.kill 发 SIGHUP，
        # terminate(force=False) 发 SIGTERM，terminate(force=True) 发 SIGKILL
        # -------------------------------------------------------------
        if proc is not None:
            try:
                if proc.isalive():
                    try:
                        os.kill(proc.pid, 1)  # SIGHUP
                    except (ProcessLookupError, OSError):
                        pass
                    deadline = time.time() + HUP_GRACE_SECONDS
                    while proc.isalive() and time.time() < deadline:
                        time.sleep(0.1)
                    if proc.isalive():
                        proc.terminate(force=True)
            except Exception:
                logger.exception("清理 PTY 进程失败")
        if reader_future is not None:
            # run_in_executor 返回 Future，取消等待中的阻塞读
            try:
                reader_future.cancel()
            except Exception:
                pass
        _release_session_slot()
        try:
            await websocket.close()
        except Exception:
            pass

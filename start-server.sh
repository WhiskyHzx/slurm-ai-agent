#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Slurm AI Agent 服务启动脚本（Unix Domain Socket 模式）
#
# 安全设计：服务不监听任何 TCP 端口，只绑定 home 目录内的 Unix socket。
# 共享登录节点上其他用户既扫不到端口，也无法穿越 700 权限的家目录连接
# socket，访问控制完全由文件系统权限（内核强制）实现。
#
# 本地访问（浏览器）需配合 SSH 隧道，在本地 ~/.ssh/config 中配置：
#   Host 107.ustc.edu.cn
#     LocalForward 8080 /home/<user>/slurm-ai-agent/server.sock
# 之后浏览器访问 http://localhost:8080 即可（VS Code Remote-SSH 会自动
# 执行该转发规则；修改 config 后需先 ssh -O exit 断开旧主连接再重连）。
#
# 用法：
#   ./start-server.sh                  # 前台准备，后台常驻运行
#   SOCKET_PATH=/tmp/x.sock ./start-server.sh   # 自定义 socket 路径
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

SOCKET_PATH="${SOCKET_PATH:-$PROJECT_DIR/server.sock}"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="${LOG_FILE:-$LOG_DIR/slurm-ai-agent-uds.log}"
mkdir -p "$LOG_DIR"

# 选择 Python 解释器：.venv 优先，其次 miniconda，最后系统 python3
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  PYTHON="$PROJECT_DIR/.venv/bin/python"
elif [ -x "$HOME/miniconda3/bin/python" ]; then
  PYTHON="$HOME/miniconda3/bin/python"
else
  PYTHON="$(command -v python3)"
fi

# 停掉已在运行的本服务实例（按 socket 路径精确匹配，不误伤其他进程）
pkill -u "$(id -un)" -f "uvicorn server.app:app --uds $SOCKET_PATH" 2>/dev/null || true
sleep 1

# 清理残留 socket 文件（uvicorn 无法绑定已存在的路径）
rm -f "$SOCKET_PATH"

setsid nohup "$PYTHON" -m uvicorn server.app:app --uds "$SOCKET_PATH" \
  > "$LOG_FILE" 2>&1 < /dev/null &
chmod 600 "$LOG_FILE" 2>/dev/null || true

echo "已启动: PID $!  socket: $SOCKET_PATH"
echo "日志:   $LOG_FILE"
echo "服务初始化约需 40 秒，健康检查命令："
echo "  curl --unix-socket $SOCKET_PATH http://localhost/api/dashboard"

# 等 socket 文件出现后立即收紧权限（uvicorn 默认创建为 666）
for _ in $(seq 1 60); do
  if [ -S "$SOCKET_PATH" ]; then
    chmod 600 "$SOCKET_PATH"
    echo "socket 权限已收紧为 600"
    break
  fi
  sleep 1
done

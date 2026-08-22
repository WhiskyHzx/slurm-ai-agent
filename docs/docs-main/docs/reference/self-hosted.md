---
page_type: how-to
audience: intermediate
status: stable
maintainers:
  - name: docs-team
graph:
  next:
    - reference/ssh-setup.md
icon: material/server
---

# 服务器部署运行

本页面说明把控制台服务部署到集群登录节点并通过浏览器访问的完整流程。前置条件见《SSH 连接配置》。

## 准备条件

1. 能通过 SSH 登录 `107.ustc.edu.cn`；
2. 服务器上的项目目录中已配置 `.env`；
3. 服务器上已创建服务运行的虚拟环境并安装依赖。

## 配置 `.env`

在项目根目录创建 `.env`，至少包含：

```env
LLM_API_KEY=你的大模型APIKey
LLM_MODEL=deepseek-chat
```

Slurm 认证无需提前写入：服务运行在登录节点上时，遇到认证失败会自动执行 `scontrol token lifespan=86400` 刷新令牌。

可选配置：

```env
SLURM_REMOTE_PROJECTS_BASE=~/projects
SLURM_UPLOAD_MAX_BYTES=2147483648
```

## 安装依赖

服务的自身依赖固定使用 venv + pip 管理（conda 只服务于用户项目的科学计算环境）：

```bash
cd ~/slurm-ai-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 启动服务

```bash
cd ~/slurm-ai-agent
source .venv/bin/activate
PYTHONPATH=. python -m uvicorn server.app:app --host 0.0.0.0 --port 8080
```

仅允许 SSH 隧道访问时，把 `--host` 改为 `127.0.0.1`。

## 浏览器访问

服务监听 `127.0.0.1:8080` 时，在本地建立 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 107.ustc.edu.cn
```

然后浏览器打开 `http://127.0.0.1:8080`。若服务器安全策略允许直接访问对应端口，也可使用服务器提供的访问地址。

## 文件上传

在页面左侧项目栏使用上传入口，选择文件或文件夹后，服务会：

1. 接收浏览器上传的文件流；
2. 在服务器临时目录暂存；
3. 创建（或复用）`~/projects/<项目名>` 目录与项目专属 conda 环境；
4. 把文件按原目录结构拷入项目目录（同名覆盖、其余保留）。

上传完成后页面会显示项目目录路径。提交作业时，智能助手以该目录作为代码与数据位置。

## 代码同步

页面上传适合首次提交或整包更新。频繁修改代码时，推荐用 rsync 同步：

```bash
rsync -az --exclude .git --exclude __pycache__ 本地项目/ 107.ustc.edu.cn:~/projects/<项目名>/
```

长时间训练任务应定期保存 checkpoint，重启作业后从断点恢复，避免浪费 GPU 时间。

## 常见问题

### Slurm API 认证失败

确认服务运行在可执行 `scontrol` 的登录节点上：

```bash
scontrol token lifespan=86400
```

该命令失败时，服务无法自动刷新 Slurm 令牌。

### LLM 调用失败

检查 `.env` 中 `LLM_API_KEY` 与 `LLM_MODEL`；模型不可用时换用平台当前支持的模型。

### 文件上传失败

确认项目根目录可写：

```bash
mkdir -p ~/projects/test-write
```

无权限时调整 `.env` 中的 `SLURM_REMOTE_PROJECTS_BASE`。

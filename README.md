# Slurm AI Agent

面向 USTC 107 算力平台的 Slurm 智能助手。项目运行在算力平台登录节点上，提供 Web 控制台和智能体聊天窗口，用于查看资源/作业状态、创建计算项目、上传文件、生成提交前建议报告，并在用户确认后继续准备 Slurm 作业提交。

## 功能概览

- 资源看板：展示节点、GPU/CPU 使用情况、作业列表和当前用户作业。
- 智能助手：通过 OpenAI 兼容接口调用大模型，支持 Function Calling 调用 Slurm 工具。
- Slurm 工具：查询作业、查询节点/QoS、提交/取消作业、读取日志、生成 sbatch 脚本。
- 知识库检索：基于 embedding 向量检索，从 107 平台文档中检索与用户问题最相关的片段。
- 项目工作流：新建作业目录、记录用户需求、自动准备 per-project conda 环境。
- 文件上传：浏览器选择文件或文件夹，服务端打包、SHA256 校验并解压到项目目录。
- 提交前报告：读取项目目录、用户需求记录和可阅读源码/脚本/配置文本，生成依赖和算力配置建议。
- 确认执行：报告生成后由用户确认，再交给智能体继续生成提交命令或提交作业。

## 项目结构

```text
slurm-ai-agent/
├── agent/
│   ├── agent_loop.py          # Function Calling 主循环和终端交互
│   ├── llm_provider.py        # OpenAI 兼容 LLM 客户端
│   └── tools_registry.py      # Slurm/知识库/模板工具定义与执行调度
├── config/
│   ├── settings.py            # API、模型、分区和默认参数
│   └── templates/             # sbatch 脚本模板
├── core/
│   ├── file_transfer.py       # 项目目录、上传归档、SHA 校验、conda 环境创建
│   ├── knowledge_base.py      # 文档知识库向量检索（embedding）
│   ├── slurm_client.py        # Slurm REST API 客户端和 JWT 刷新
│   └── template_engine.py     # 作业脚本模板渲染
├── server/
│   ├── app.py                 # FastAPI 后端、Dashboard API、项目/上传/报告 API
│   └── static/index.html      # Web 控制台和智能体界面
├── docs/docs-main/            # 107 平台文档镜像，用于知识库
├── evaluation/                # API 能力确认、比赛方案评估和使用说明
├── requirements.txt
└── README.md
```

## 运行环境

- Python 3.10+
- Miniconda/conda 可用，用于为每个项目创建独立环境
- 在 107 算力平台登录节点运行，例如 `tradmin-01` / `tradmin-02`
- 可访问 Slurm REST API：`http://107.ustc.edu.cn:6820`
- 可访问 LLM API：`https://api.llm.ustc.edu.cn/v1`（对话 + embedding）

## 准备 Miniconda（conda）环境

本项目为每个项目创建独立 conda 环境。如果登录节点上还没有 conda/mamba，请先安装 Miniconda。

### 安装 Miniconda

```bash
cd ~
wget https://mirrors.ustc.edu.cn/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p "$HOME/miniconda3"
~/miniconda3/bin/conda init bash
source ~/.bashrc
```

> 如果 conda 提示需要接受 Anaconda 服务条款，先执行：
>
> ```bash
> conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
> conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
> ```

### 配置软件源（可选，加速国内下载）

```bash
cat > ~/.condarc <<'EOF'
channels:
  - https://mirrors.ustc.edu.cn/anaconda/cloud/conda-forge
  - https://mirrors.ustc.edu.cn/anaconda/pkgs/main
  - https://mirrors.ustc.edu.cn/anaconda/pkgs/r
show_channel_urls: true
EOF

pip config set global.index-url https://mirrors.ustc.edu.cn/pypi/web/simple
```

更完整的 conda 环境配置说明见：`docs/docs-main/docs/basics/environments.md`。

## 安装

```bash
cd slurm-ai-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> **环境边界**：服务自身的依赖固定用 venv + pip 管理（如上）；conda 只服务于用户项目的科学计算环境（`<项目>/.slurm-agent/conda-env`），不要把服务依赖装进 conda base，也不要用 conda base 的 Python 跑本服务。

## 配置

项目只从环境变量或本地 `.env` 读取密钥，`.env` 不会提交到 Git。

```bash
export LLM_API_KEY="你的学校大模型 API Key"
export LLM_MODEL="deepseek-v4-flash"

# 可选。默认会在需要时通过 scontrol token 自动刷新。
export SLURM_JWT="$(scontrol token lifespan=86400 | sed 's/SLURM_JWT=//')"
```

常用可选变量：

```bash
export SLURM_API_BASE_URL="http://107.ustc.edu.cn:6820"
export SLURM_REMOTE_PROJECTS_BASE="~/projects"
export SLURM_CONDA_EXE="$HOME/miniconda3/bin/conda"
export SLURM_PROJECT_CONDA_PYTHON="3.10"
export SLURM_UPLOAD_MAX_BYTES="2147483648"
```

知识库检索使用 embedding 向量检索，相关可选变量：

```bash
export EMBEDDING_MODEL="qwen3-embedding"   # 默认 qwen3-embedding
export RERANKER_MODEL="qwen3-reranker"    # 预留，未启用
export EMBEDDING_CACHE_DIR=".embedding_cache"  # 向量缓存目录
```

> 知识库向量检索会在首次使用时自动构建索引并缓存到本地，之后直接读缓存；文档更新后可调用 `rebuild_index()` 或删除缓存目录重新构建。


## 启动 Web 控制台

服务以 **Unix Domain Socket 模式**启动：不监听任何 TCP 端口，只绑定项目目录内的
`server.sock`。共享登录节点上其他用户既扫不到端口，也无法穿越 700 权限的家目录
连接 socket——访问控制由文件系统权限在内核层强制执行。

```bash
cd slurm-ai-agent
./start-server.sh   # 自动选择 .venv/miniconda 解释器，后台常驻运行

# 等价的手动启动方式：
# source .venv/bin/activate
# PYTHONPATH=. uvicorn server.app:app --uds "$PWD/server.sock"
# chmod 600 server.sock   # uvicorn 默认建 666，必须收紧
```

启动后约需 40 秒初始化，验证健康：

```bash
curl --unix-socket server.sock http://localhost/api/dashboard
```

### 本地浏览器访问（SSH 隧道）

浏览器不能直接访问 Unix socket，需在本地 `~/.ssh/config` 中配置转发：

```
Host 107.ustc.edu.cn
  LocalForward 8080 /home/<user>/slurm-ai-agent/server.sock
```

之后浏览器访问 `http://localhost:8080`。VS Code Remote-SSH 会自动执行该转发
规则；注意修改 config 后需先 `ssh -O exit 107.ustc.edu.cn` 断开旧主连接
（若启用了 ControlPersist），再重连才能生效。

> 历史版本曾用 `--host 0.0.0.0` / `--host 127.0.0.1` 监听 TCP 8080，在多用户
> 共享的登录节点上存在被同节点其他用户直接访问的风险（回环地址全机共享，
> TCP 端口无属主概念），已废弃。禁止再用任何 `--host` 方式启动。

## Web 工作流

1. 点击右上角 `+ 新作业`。
2. 填写作业目录名称、环境依赖要求、算力特别需求。
3. 后端创建 `~/projects/<作业目录名称>`，并初始化 `<作业目录>/.slurm-agent/conda-env`。
4. 在智能体输入框上方点击 `文件` 或 `文件夹` 上传项目内容。
5. 点击 `建议报告`。
6. 后端读取：
   - 作业目录名称和作业目录
   - 用户输入记录 `PROJECT_NOTES.txt`
   - 项目目录摘要
   - 可直接阅读的文本/源码/脚本/配置文件
   - conda 包查询结果
7. 智能体返回依赖安装建议、算力配置建议、输出路径规范、待确认问题和 sbatch 草案。
8. 如需修改，在输入框补充意见后再次点击 `建议报告`。
9. 确认无误后点击 `确认执行`，智能体继续准备提交命令或调用提交工具。

输出路径规范：

- 程序结果：直接写在当前运行目录（项目根或所选子目录）
- Slurm stdout：`logs/<作业名>-%j.out`
- Slurm stderr：`logs/<作业名>-%j.err`

## 终端模式

```bash
PYTHONPATH=. python agent/agent_loop.py -i
```

可直接输入自然语言问题，例如：

```text
帮我看看 P107-RTX5090 分区有哪些正在运行的作业
帮我生成一个单 GPU PyTorch 训练脚本
读取 40301 的错误日志并分析失败原因
```

## 主要 API

- `GET /`：Web 控制台
- `GET /health`：健康检查
- `GET /api/dashboard`：资源和作业概览
- `POST /api/slurm/refresh`：在登录节点刷新 Slurm JWT
- `POST /api/projects`：创建作业目录、初始化项目 Conda 环境、记录需求
- `POST /api/files/upload`：上传文件/文件夹并 SHA256 校验
- `POST /api/projects/report`：生成提交前建议报告
- `POST /chat`：智能体 SSE 聊天接口
- `POST /reset`：重置智能体上下文

## 安全说明

- 不提交 `.env`、Token、API Key、日志、运行输出和上传缓存。
- 上传路径会拒绝绝对路径、`..` 和 `.slurm-agent` 目录。
- 上传归档会在服务端保存后重新计算 SHA256。
- 每个项目使用独立 conda 环境，避免污染全局环境。
- 报告阶段不会自动安装依赖或提交作业，必须由用户点击 `确认执行`。

## 验证

```bash
python3 -m py_compile server/app.py core/file_transfer.py core/slurm_client.py agent/agent_loop.py agent/tools_registry.py
```

前端是纯 HTML/CSS/JS，无构建步骤；可以通过浏览器访问运行中的 FastAPI 服务验证。

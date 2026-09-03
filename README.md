# Slurm AI Agent

Slurm AI Agent 是面向 USTC 107 算力平台的智能作业助手。它部署在平台登录节点上，将项目文件、Python 环境、Slurm 资源、作业提交和运行结果集中到一个 Web 工作区中，并通过大模型帮助用户理解平台、准备任务和排查问题。

项目希望降低 Slurm 的使用门槛，同时保留批处理作业应有的可控性：用户始终决定安装哪些依赖、申请多少资源以及何时提交作业；系统负责整理上下文、生成建议、校验配置，并把任务从准备阶段持续跟踪到结果分析。

## 功能概览

- **集群资源与作业看板**：集中展示节点、CPU/GPU 使用情况、实时作业和个人历史作业，帮助用户在提交前了解资源状态，在提交后掌握任务进展。
- **项目化工作空间**：以 `~/projects` 下的项目目录组织代码、数据、会话和运行结果。一个项目可以包含多个独立运行目录，各目录拥有自己的对话上下文，并共享项目级 Python 环境。
- **智能助手**：支持自然语言查询集群信息、解释 Slurm 概念、检索平台文档、生成作业命令、读取项目文件和分析运行日志。模型可在 Web 界面中切换，并通过工具调用获取真实的项目与集群上下文。
- **环境与依赖管理**：为每个项目准备独立的 Conda 环境，分析依赖清单、源码和配置文件，给出结构化依赖建议，并在用户确认后完成安装和验证。
- **作业规划与提交**：用户可以选择账户、分区、QoS、CPU、GPU、内存和时限，结合 AI 建议或作业模板准备任务。系统在提交前统一校验运行目录、资源配置和执行命令。
- **作业模板**：提供单卡 PyTorch、多卡 DDP、CPU 批处理、Job Array、Jupyter 和通用脚本等模板，也支持保存个人模板供不同项目复用。
- **监控、报告与结果浏览**：持续跟踪已提交作业；任务结束后汇总 Slurm 状态和输出日志，生成成功或失败报告，并支持在线浏览文本结果或打包下载输出目录。
- **文件、终端与平台文档**：内置文件管理器、登录节点终端和 107 平台文档阅读器，覆盖上传、编辑、下载、命令行操作以及独立的文档问答场景。
- **面向共享节点的安全访问**：服务默认绑定 Unix Domain Socket，通过 SSH 隧道在本地浏览器访问，避免直接暴露登录节点上的 TCP 端口。

## 典型使用场景

- 第一次使用 Slurm，希望从项目上传、环境准备到作业提交获得完整引导。
- 运行课程作业、深度学习训练、数值计算、数据处理或批量实验。
- 不确定该选择哪个分区、QoS 或资源规格，需要结合平台现状进行配置。
- 项目依赖复杂，希望先识别并确认依赖，再安装到隔离环境中。
- 作业失败后，需要结合任务状态、标准输出和错误日志定位原因。
- 希望在浏览器中统一管理远端文件、终端、平台文档和历史结果。

## Web 控制台

Web 控制台由三个协同区域组成：

- 左侧管理项目、运行目录和文件，也可切换到平台文档或完整文件管理器。
- 中间是智能助手，用于讨论需求、检查依赖、准备作业和查看分析结果。
- 右侧展示节点与作业状态，提供资源详情和个人历史作业。

顶部入口可以打开登录节点终端；帮助文档和文件管理器各自拥有独立问答会话，不会与项目作业的上下文混在一起。

## 用户工作流程

1. **进入控制台并选择项目**  
   通过 SSH 隧道打开 Web 控制台，选择已有项目，或创建一个新的项目工作空间。需要时可在项目下建立多个运行目录，分别组织不同数据集、实验或任务。

2. **准备代码、数据和环境**  
   上传项目文件，或使用文件管理器和内置终端进行整理。系统为项目维护独立 Conda 环境，同一项目中的多个运行目录可以共享已经安装的依赖。

3. **与智能助手确认需求**  
   描述任务目标、依赖要求或算力需求。助手可以读取当前项目结构、搜索平台文档并查询集群状态，让建议建立在实际上下文之上。

4. **检查并安装依赖**  
   运行依赖检查，审阅系统识别出的包、版本要求和安装方式。确认需要安装的内容后，系统将它们安装到当前项目环境，并在界面中展示进度与结果。

5. **布置作业**  
   选择运行目录和 Slurm 资源，填写或生成实际执行命令。常见任务可以从内置模板或个人模板开始，也可以让助手结合项目文件生成建议。

6. **确认并提交**  
   检查作业名称、资源规格、运行目录和命令。确认后由服务统一生成并提交作业脚本，Web 提交和智能助手提交遵循相同的校验规则。

7. **跟踪作业状态**  
   在资源面板中查看排队、运行和结束状态；也可以向助手询问作业详情、调度优先级、资源占用或历史记录。

8. **查看和分析结果**  
   作业结束后，从会话消息进入报告阅读器，查看日志和输出文件，或下载完整结果。失败任务可以继续交给助手分析并给出修改建议。

默认情况下，作业日志、提交脚本和分析报告保存在所选运行目录的 `logs/` 中；程序生成的模型、指标和其他产物由实际命令决定，建议统一写入 `runs/` 或其他固定输出目录。

## 工作空间与环境模型

项目数据默认位于 `~/projects/<项目名>`：

```text
~/projects/<项目名>/
├── activate.sh                    # 激活项目环境
├── <代码、数据与配置>
├── <运行目录>/                    # 可选；用于组织不同实验
├── logs/                          # 作业脚本、标准输出、错误日志和报告
├── runs/                          # 建议用于保存训练或计算产物
└── .slurm-agent/
    └── conda-env/                 # 项目独立 Conda 环境
```

Web 服务和用户项目使用不同的 Python 环境：

- 仓库根目录的 `.venv` 只运行 Slurm AI Agent 服务。
- 每个用户项目的 `.slurm-agent/conda-env` 只承载该项目的计算依赖。

这种划分可以避免污染系统 Python 或 Conda base，也让不同项目之间的依赖相互隔离。

## 快速开始

### 1. 运行要求

- Python 3.10 或更高版本。
- Miniconda、Miniforge 或 Anaconda，用于创建项目环境。
- 在 USTC 107 算力平台登录节点上运行，例如 `tradmin-01` 或 `tradmin-02`。
- 能够访问平台 Slurm REST API 和 OpenAI 兼容的大模型 API。

如果登录节点还没有 Conda，可以安装 Miniconda：

```bash
cd ~
wget https://mirrors.ustc.edu.cn/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p "$HOME/miniconda3"
~/miniconda3/bin/conda init bash
source ~/.bashrc
```

如果 Conda 提示需要接受 Anaconda 服务条款，请按提示接受对应 channel 的条款。更完整的环境说明见 [环境配置文档](docs/docs-main/docs/basics/environments.md)。

### 2. 安装服务依赖

```bash
cd slurm-ai-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

不要把服务依赖安装到 Conda base。项目计算环境会在创建 Web 项目时单独准备。

### 3. 配置服务

密钥可以通过环境变量或仓库根目录下的 `.env` 提供；`.env` 已被 Git 忽略。

```bash
export LLM_API_KEY="你的学校大模型 API Key"
export LLM_MODEL="deepseek-v4-flash"

# 可选；未设置时，服务会在需要时通过 scontrol token 获取或刷新
export SLURM_JWT="$(scontrol token lifespan=86400 | sed 's/SLURM_JWT=//')"
```

默认服务地址已经针对 107 平台配置。如部署环境不同，可通过下文的配置项覆盖。

### 4. 启动服务

```bash
cd slurm-ai-agent
./start-server.sh
```

启动脚本会在后台运行 FastAPI 服务，并创建权限受限的 `server.sock`。可以通过以下命令检查服务是否可用：

```bash
curl --unix-socket server.sock http://localhost/health
```

服务启动日志写入 `server.log`。

### 5. 从本地浏览器访问

浏览器不能直接访问远端 Unix socket，需要在本地 `~/.ssh/config` 中配置转发：

```sshconfig
Host 107.ustc.edu.cn
  LocalForward 8080 /home/<用户名>/slurm-ai-agent/server.sock
```

重新建立 SSH 连接后，在本地浏览器打开 <http://localhost:8080>。如果启用了 `ControlPersist`，修改 SSH 配置后需要先关闭旧主连接，再重新连接：

```bash
ssh -O exit 107.ustc.edu.cn
```

> 不要改用 `uvicorn --host ...` 暴露 TCP 端口。共享登录节点上的本机端口并不只属于当前用户，项目默认的 Unix socket 方式提供了更合适的访问边界。

## 在命令行中使用项目环境

通过 Web 控制台安装的依赖位于项目自己的 Conda 环境中。在登录节点手动运行项目脚本时，推荐先使用项目根目录下的激活脚本：

```bash
cd ~/projects/<项目名>
source activate.sh
python your_script.py
```

也可以直接调用项目环境中的 Python：

```bash
~/projects/<项目名>/.slurm-agent/conda-env/bin/python your_script.py
```

如果出现 `ModuleNotFoundError`，先运行 `which python`，确认路径中包含 `.slurm-agent/conda-env`。

## 命令行智能助手

除了 Web 控制台，也可以直接启动交互式 Agent：

```bash
PYTHONPATH=. python agent/agent_loop.py -i
```

示例问题：

```text
查看 P107-RTX5090 分区当前有哪些作业
为这个项目生成一个单 GPU PyTorch 训练脚本
读取作业 40301 的错误日志并分析失败原因
```

命令行模式主要适合查询、诊断和脚本生成；完整的项目准备、依赖确认和结果浏览流程建议使用 Web 控制台。

## 配置参考

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `LLM_API_KEY` | 无 | OpenAI 兼容模型服务的 API Key，必填 |
| `LLM_BASE_URL` | `https://api.llm.ustc.edu.cn/v1` | 对话与模型列表服务地址 |
| `LLM_MODEL` | `deepseek-v4-flash` | 默认对话模型；也可在 Web 界面中切换 |
| `EMBEDDING_MODEL` | `qwen3-embedding` | 平台文档向量检索模型 |
| `SLURM_API_BASE_URL` | `http://107.ustc.edu.cn:6820` | Slurm REST API 地址 |
| `SLURM_API_PREFIX` | `/slurm/v0.0.41` | Slurm REST API 版本前缀 |
| `SLURM_JWT` | 自动获取 | Slurm REST API 认证令牌 |
| `SLURM_REMOTE_PROJECTS_BASE` | `~/projects` | Web 项目工作空间根目录 |
| `SLURM_CONDA_EXE` | 自动探测 | Conda 可执行文件路径 |
| `SLURM_PROJECT_CONDA_PYTHON` | `3.10` | 新建项目环境的 Python 版本 |
| `SLURM_UPLOAD_MAX_BYTES` | `2147483648` | 单次项目上传的总大小上限 |

还可以配置模型输出、安装超时、上传文件数量、Conda channel 和文件预览上限等参数，具体定义见 `config/settings.py`、`core/file_transfer.py` 和 `server/app.py`。

## 系统架构

```text
本地浏览器
    │ SSH 隧道
    ▼
Unix Domain Socket
    │
    ▼
FastAPI 服务 ── Web 控制台 / 文件管理 / 终端 / 作业监控
    ├── Agent 与 OpenAI 兼容模型
    ├── 平台文档知识库
    ├── 项目目录与 Conda 环境
    └── Slurm REST API 与只读 CLI 工具
```

核心模块：

```text
slurm-ai-agent/
├── agent/                       # 智能体循环、模型客户端和工具注册
├── config/                      # 运行配置、模型选择和内置作业模板
├── core/                        # Slurm、知识库、依赖、文件与模板能力
├── server/
│   ├── app.py                   # FastAPI 后端与主要业务流程
│   ├── terminal.py              # WebSocket PTY 终端
│   └── static/                  # 单页 Web 控制台与 xterm.js 资源
├── docs/docs-main/docs/         # 内置 107 平台使用文档
├── evaluation/                  # 平台能力实测和评估资料
├── start-server.sh              # Unix socket 启动脚本
└── requirements.txt             # 服务端 Python 依赖
```

## API 与开发

FastAPI 会提供交互式接口文档。服务启动后，可通过同一 SSH 隧道访问 <http://localhost:8080/docs>。

后端接口按能力分为以下几组：

- 集群资源、Slurm 认证和历史作业。
- 项目、会话、运行目录、上传和依赖管理。
- 作业配置、模板、提交、监控、报告和结果下载。
- 平台文档、用户文件管理和 WebSocket 终端。
- 模型列表、模型切换和 SSE 流式智能体对话。

前端使用原生 HTML、CSS 和 JavaScript，无需额外构建步骤。服务端依赖见 `requirements.txt`；API 实现集中在 `server/app.py`。

## 安全边界

- API Key、Slurm Token 和其他密钥只从环境变量或本地 `.env` 读取，不应提交到仓库。
- Web 服务默认只监听项目目录内的 Unix socket，并将 socket 权限限制为当前用户可访问。
- 文件操作限制在允许的工作目录中，并保护项目内部状态目录等关键路径。
- 每个项目使用独立 Conda 环境，避免修改系统 Python、Conda base 或其他项目环境。
- 依赖安装、作业提交和作业取消属于有副作用的操作，需要由用户明确发起。
- 智能体提交作业时使用与 Web 控制台相同的资源和路径校验流程，不能通过生成任意完整脚本绕过提交边界。

## 相关文档

- [107 平台快速开始](docs/docs-main/docs/quickstart.md)
- [环境配置](docs/docs-main/docs/basics/environments.md)
- [提交任务](docs/docs-main/docs/basics/jobs.md)
- [Slurm 速查](docs/docs-main/docs/basics/slurm.md)
- [常见问题](docs/docs-main/docs/basics/faq.md)
- [平台 API 与命令能力实测](evaluation/107-api-capability-report.md)

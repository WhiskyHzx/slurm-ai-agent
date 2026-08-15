# 算力平台答疑与作业脚本生成智能体

"一〇七杯"算力与智能体开发大赛 · 智能体开发类命题

基于大模型 Function Calling 的算力平台智能助手，支持自然语言查询作业、提交/取消作业、脚本生成、报错诊断、日志分析等功能。

提供**终端交互模式**和 **Web 聊天界面**两种使用方式。

## 项目结构

```
slurm-ai-agent/
├── agent/                      # 智能体核心
│   ├── agent_loop.py           # Function Calling 主循环 + 终端交互
│   ├── llm_provider.py         # LLM 调用封装（OpenAI 兼容接口）
│   └── tools_registry.py       # 工具注册表（12 个工具定义 + 执行调度）
├── core/                       # 平台 API 封装
│   ├── slurm_client.py         # Slurm REST API 客户端（含 Token 管理）
│   ├── knowledge_base.py       # 知识库检索（平台文档 RAG）
│   └── template_engine.py      # 作业脚本模板引擎
├── config/                     # 配置
│   ├── settings.py             # 全局配置（分区、API 地址、LLM 参数等）
│   └── templates/              # 作业脚本模板（6 个模板）
│       ├── index.json          # 模板索引
│       ├── pytorch_single_gpu.json
│       ├── pytorch_ddp.json
│       ├── jupyter_interactive.json
│       ├── cpu_batch.json
│       ├── job_array.json
│       └── simple_script.json
├── server/                     # Web 服务
│   ├── app.py                  # FastAPI 后端（SSE 流式 API）
│   └── static/
│       └── index.html          # 聊天界面（纯 HTML/CSS/JS）
├── docs/docs-main/docs/        # 平台知识库文档（17 个 .md 文件）
├── tests/                      # 单元测试
├── requirements.txt            # Python 依赖
└── README.md                   # 本文件
```

## 快速开始

### 1. 环境要求

- Python 3.10+
- 在算力平台登录节点（tradmin-01 / tradmin-02）上运行
- 网络可访问 `http://107.ustc.edu.cn:6820`（Slurm REST API）和 `https://api.llm.ustc.edu.cn`（LLM API）

### 2. 安装依赖

```bash
cd slurm-ai-agent
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
# 获取 Slurm JWT Token（有效期 1 天）
export SLURM_JWT=$(scontrol token lifespan=86400 | sed 's/SLURM_JWT=//')

# 设置 LLM API Key
export LLM_API_KEY=sk-你的APIKey
```

> ⚠️ Token 和 API Key 仅从环境变量读取，不会硬编码或提交到 Git。

### 4. 启动 Web 聊天界面（推荐）

```bash
PYTHONPATH=. uvicorn server.app:app --host 0.0.0.0 --port 8080
```

启动后通过以下方式访问：

| 方式 | 地址 | 适用场景 |
|------|------|----------|
| **VS Code 端口转发**（推荐） | 点击终端中弹出的 `[算力平台智能助手](http://localhost:8080/)` 链接 | VS Code Remote 远程开发时使用 |
| 直接访问 | `http://<服务器IP>:8080` | 与服务器在同一网络时使用 |

> 💡 VS Code 会自动将远程服务器的 8080 端口映射到本地 `localhost:8080`，无需额外配置。获取服务器 IP：`hostname -I \| awk '{print $1}'`

### 5. 启动终端交互模式

```bash
PYTHONPATH=. python agent/agent_loop.py -i
```

交互命令：
- 直接输入问题，如"帮我看看 P107-RTX5090 分区有什么作业"
- `reset` — 重置对话历史
- `quit` / `exit` — 退出

## 可用工具（12 个）

### 作业管理

| 工具 | 功能 | API 端点 |
|------|------|----------|
| `list_jobs` | 查询作业列表，可按分区过滤 | `GET /slurm/v0.0.41/jobs` |
| `get_job` | 查询单个作业详情 | `GET /slurm/v0.0.41/job/{id}` |
| `submit_job` | 提交新作业 | `POST /slurm/v0.0.41/job/submit` |
| `cancel_job` | 取消指定作业 | `DELETE /slurm/v0.0.41/job/{id}` |
| `get_jobs_history` | 查询历史作业（含已完成/失败） | `GET /slurmdb/v0.0.41/jobs` |
| `read_job_log` | 读取作业输出/错误日志文件 | REST + 本地文件系统 |

### 集群信息

| 工具 | 功能 | API 端点 |
|------|------|----------|
| `get_diag` | 查看集群整体统计 | `GET /slurm/v0.0.41/diag` |
| `get_nodes` | 查询节点详细信息 | `GET /slurm/v0.0.41/nodes` |
| `get_qos` | 查询 QoS 资源配额 | `GET /slurmdb/v0.0.41/qos` |

### 知识库与脚本生成

| 工具 | 功能 | 说明 |
|------|------|------|
| `search_knowledge` | 搜索平台知识库 | 基于 17 篇平台文档的关键词检索 |
| `list_templates` | 列出可用作业脚本模板 | 6 个模板（PyTorch/CPU/Jupyter/Job Array 等） |
| `generate_script` | 根据模板生成 sbatch 脚本 | 支持自定义参数填充 |

## 分区说明

| 分区 | 节点 | GPU | 账户 | 最大节点 |
|------|------|-----|------|:---:|
| P107-RTX5090 | anode[01-15] | RTX 5090 × 8 | competition | 15 |
| P107-A100 | anode[16-26] | A100 80G × 8 | competition | 2 |
| GPU-RTX5090 | anode[01-15] | RTX 5090 × 8 | demo_admin, cmet | 15 |
| GPU-A100 | anode[16-26] | A100 80G × 8 | — | 2 |
| CPU-6530 | anode[01-15] | — | demo_admin, cmet | 15 |
| CPU-8358P | anode[16-26] | — | demo_admin, cmet | 2 |
| Students | — | — | stu, stu001 等 | 2 |

## 开发阶段

| 阶段 | 内容 | 状态 |
|:---:|------|:---:|
| 1 | slurm_client.py — REST API 封装 + Token 管理 | ✅ 完成 |
| 2 | Function Calling 核心循环 + 工具注册 | ✅ 完成 |
| 3 | 知识库 RAG + 脚本模板引擎 + 报错诊断 | ✅ 完成 |
| 4 | 日志/资源分析与优化建议 | ⏭️ 跳过（平台已有 Grafana 监控） |
| 5 | Web 聊天界面 + 演示 | ✅ 完成 |

## 技术栈

- **语言**：Python 3.12+
- **LLM SDK**：openai >= 1.0.0（兼容 OpenAI 接口）
- **Web 框架**：FastAPI + Uvicorn（SSE 流式响应）
- **HTTP 客户端**：requests
- **LLM 模型**：deepseek-v4-pro（通过 https://api.llm.ustc.edu.cn）
- **平台 API**：Slurm REST API v0.0.41 (slurmrestd + slurmdbd)
- **调度系统**：Slurm 25.11.2

## 安全注意事项

- Token 通过 `SLURM_JWT` 环境变量传入，绝不硬编码
- LLM API Key 通过 `LLM_API_KEY` 环境变量传入
- `.env` 和 `*.token` 文件已在 `.gitignore` 中排除
- 所有 HTTP 请求统一封装在 `core/slurm_client.py` 中
- 取消作业操作有二次确认机制
- Token / API Key 绝不会被传入外部大模型消息内容
"""
config/settings.py — 项目全局配置。

所有可调参数集中管理，避免散落在各模块中。
"""

import os
from pathlib import Path


def _load_dotenv() -> None:
    """轻量加载项目根目录 .env，避免额外引入 python-dotenv 依赖。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

# ---------------------------------------------------------------------------
# 算力平台 REST API
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("SLURM_API_BASE_URL", "http://107.ustc.edu.cn:6820")
API_PREFIX = os.environ.get("SLURM_API_PREFIX", "/slurm/v0.0.41")

# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------
# Token 默认有效期（秒），用于 scontrol token lifespan= 参数
DEFAULT_TOKEN_LIFESPAN = 86400  # 1 天
# 是否开启 Token 过期自动刷新（需在登录节点运行）
AUTO_REFRESH_TOKEN = True

# ---------------------------------------------------------------------------
# 分区（Partition / Queue）
# ---------------------------------------------------------------------------
# 平台现有分区列表（以 sinfo 实时输出为准）
PARTITIONS = {
    "CPU-6530": "Xeon 6530 × 2, 128 核, 512 GB",
    "CPU-8358P": "Xeon 8358P × 2, 128 核, 1 TB",
    "GPU-RTX5090": "RTX 5090 × 8",
    "GPU-A100": "A100 80G × 8",
    "P107-RTX5090": "比赛专用 RTX 5090 分区",
    "P107-A100": "比赛专用 A100 分区",
    "Students": "学生分区",
}

# 默认提交分区
DEFAULT_PARTITION = "P107-RTX5090"

# ---------------------------------------------------------------------------
# 作业默认参数
# ---------------------------------------------------------------------------
DEFAULT_JOB_NAME = "api-job"
DEFAULT_NODES = 1
DEFAULT_TIME_LIMIT_MINUTES = 60

# ---------------------------------------------------------------------------
# HTTP 请求
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT = 30  # 秒

# ---------------------------------------------------------------------------
# 大模型 (LLM) 配置
# ---------------------------------------------------------------------------
# API Key 从环境变量 LLM_API_KEY 读取，绝不硬编码
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.llm.ustc.edu.cn/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")        # 主力模型（Function Calling）
LLM_FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", "deepseek-v4-flash")  # 备用模型（快速/降级）
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.3"))                    # 工具调用场景建议低温度
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))
LLM_MAX_TOOL_TURNS = int(os.environ.get("LLM_MAX_TOOL_TURNS", "10"))                  # 单轮对话最多工具调用轮数，防止死循环

# ---------------------------------------------------------------------------
# Embedding（向量检索）配置
# ---------------------------------------------------------------------------
# Embedding 模型（独立于对话模型，不影响 LLM_MODEL 选择）
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "qwen3-embedding")
# 可选 reranker 模型（当前未启用。文档量增大后可启用做重排序）
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "qwen3-reranker")
# 向量缓存目录（相对项目根）
EMBEDDING_CACHE_DIR = os.environ.get(
    "EMBEDDING_CACHE_DIR",
    str(Path(__file__).resolve().parent.parent / ".embedding_cache"),
)

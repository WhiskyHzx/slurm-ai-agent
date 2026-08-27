#!/usr/bin/env python3
"""
knowledge_base.py — 平台知识库检索模块（向量检索版）。

从 docs/docs-main/docs/ 下读取所有 .md 文档，按标题分块，
用 embedding 模型将文档片段向量化，检索时计算余弦相似度，
返回最相关的文档片段。

依赖：
    - openai（复用项目已有的 OpenAI 兼容 SDK）
    - numpy（余弦相似度计算）

特性：
    - 向量缓存到磁盘，避免每次启动重复调用 embedding API
    - embedding 调用失败时自动降级到关键词检索
    - 保留 search() 对外接口，调用方无需改动
"""

import os
import re
import json
import logging
from typing import List, Dict, Optional
from pathlib import Path

from config.settings import (
    LLM_BASE_URL,
    EMBEDDING_MODEL,
    EMBEDDING_CACHE_DIR,
)

logger = logging.getLogger(__name__)

# =========================================================================
# 配置
# =========================================================================

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs" / "docs-main" / "docs"

# 排除的目录（不需要检索的）
EXCLUDE_DIRS = {"assets", "stylesheets", "contributing"}

# 最大返回片段数
MAX_RESULTS = 3

# 每个片段最大字符数（截断过长内容）
MAX_CHUNK_CHARS = 2000

# 向量缓存文件
CACHE_DIR = Path(EMBEDDING_CACHE_DIR)
CHUNKS_CACHE = CACHE_DIR / "chunks.json"
VECTORS_CACHE = CACHE_DIR / "vectors.npy"

# embedding 批量请求大小（一次 API 调用处理多少片段）
EMBED_BATCH_SIZE = 16

# 相似度阈值（低于该值的片段直接丢弃，避免返回不相关内容）
SIM_THRESHOLD = 0.3


# =========================================================================
# 文档加载与分块
# =========================================================================


def _load_all_docs(docs_dir: Path = DOCS_DIR) -> List[Dict[str, str]]:
    """
    加载所有 .md 文档，按 ## 标题分块。

    返回:
        [{"title": "文件名 / 章节标题", "content": "片段内容", "file": "相对路径"}, ...]
    """
    chunks: List[Dict[str, str]] = []

    if not docs_dir.exists():
        logger.warning("文档目录不存在: %s", docs_dir)
        return chunks

    for md_file in docs_dir.rglob("*.md"):
        # 跳过排除目录
        parts = md_file.relative_to(docs_dir).parts
        if any(p in EXCLUDE_DIRS for p in parts):
            continue

        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("读取文件失败 %s: %s", md_file, e)
            continue

        # 提取文件名（不含扩展名）作为一级标题
        rel_path = str(md_file.relative_to(docs_dir))
        file_title = rel_path.replace(".md", "").replace("/", " → ")

        # 按 ## 标题分块
        sections = re.split(r"\n(?=## )", text)

        for section in sections:
            # 提取标题行
            title_match = re.match(r"^## (.+)", section)
            if title_match:
                section_title = f"{file_title} / {title_match.group(1).strip()}"
                body = section[title_match.end():].strip()
            else:
                # 没有 ## 标题的段落，用文件名作为标题
                section_title = file_title
                body = section.strip()

            # 跳过 YAML frontmatter（--- 开头的元数据块）
            body = re.sub(r"^---\n.*?\n---\n", "", body, flags=re.DOTALL)

            # 跳过空内容
            if len(body) < 20:
                continue

            # 截断过长内容
            if len(body) > MAX_CHUNK_CHARS:
                body = body[:MAX_CHUNK_CHARS] + "\n\n...（内容过长，已截断）"

            chunks.append({
                "title": section_title,
                "content": body,
                "file": rel_path,
            })

    logger.info("文档加载完成: %d 个文件 → %d 个片段", 
                 len(set(c["file"] for c in chunks)), len(chunks))
    return chunks


# =========================================================================
# Embedding 调用
# =========================================================================


def _embed(texts: List[str]) -> List[List[float]]:
    """
    调用 embedding API 将文本列表向量化。

    参数:
        texts: 待向量化的文本列表

    返回:
        向量列表，每个向量是 float 列表
    """
    from openai import OpenAI

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("环境变量 LLM_API_KEY 未设置，无法调用 embedding API")

    client = OpenAI(api_key=api_key, base_url=LLM_BASE_URL)

    all_vectors: List[List[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        for item in resp.data:
            all_vectors.append(list(item.embedding))
    return all_vectors


def _cosine(a: List[float], b) -> float:
    import numpy as np

    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


# =========================================================================
# 向量索引构建与缓存
# =========================================================================


def _load_cache() -> Optional[tuple]:
    """从磁盘加载缓存的 chunks 和向量。缓存无效时返回 None。"""
    if not CHUNKS_CACHE.exists() or not VECTORS_CACHE.exists():
        return None
    try:
        chunks = json.loads(CHUNKS_CACHE.read_text(encoding="utf-8"))
        import numpy as np
        vectors = np.load(VECTORS_CACHE)
        if len(chunks) != len(vectors):
            logger.warning("缓存大小不一致，忽略缓存")
            return None
        return chunks, vectors
    except Exception as e:
        logger.warning("读取向量缓存失败: %s", e)
        return None


def _save_cache(chunks: List[Dict[str, str]], vectors) -> None:
    """将 chunks 和向量保存到磁盘。"""
    import numpy as np

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CHUNKS_CACHE.write_text(
        json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    np.save(VECTORS_CACHE, vectors)
    logger.info("向量缓存已保存到 %s", CACHE_DIR)


def _build_index(chunks: List[Dict[str, str]], force: bool = False):
    """
    构建向量索引。优先读缓存，缓存缺失或 force=True 时重新向量化。

    返回:
        (chunks, vectors) 元组，vectors 是 numpy 数组
    """
    import numpy as np

    if not force:
        cached = _load_cache()
        if cached is not None:
            logger.info("向量索引已加载（来自缓存）")
            return cached

    logger.info("开始向量化 %d 个片段（模型: %s）...", len(chunks), EMBEDDING_MODEL)
    texts = [c["title"] + "\n" + c["content"] for c in chunks]
    vectors = _embed(texts)
    arr = np.asarray(vectors, dtype=np.float32)
    _save_cache(chunks, arr)
    return chunks, arr


# =========================================================================
# 检索
# =========================================================================


def search(query: str, docs_dir: Optional[Path] = None) -> str:
    """
    根据用户查询检索最相关的文档片段。

    算法：向量检索（embedding 余弦相似度排序）。

    参数:
        query: 用户查询文本
        docs_dir: 文档目录，默认 DOCS_DIR

    返回:
        格式化的检索结果字符串，可直接注入 LLM context
    """
    if docs_dir is None:
        docs_dir = DOCS_DIR

    chunks = _load_all_docs(docs_dir)
    if not chunks:
        return "（知识库为空，无法检索相关信息）"

    try:
        chunks, vectors = _build_index(chunks)
        return _search_vector(query, chunks, vectors)
    except Exception as e:
        logger.warning("向量检索失败，降级到关键词检索: %s", e)
        return _search_keyword_fallback(query, chunks)


def _search_vector(query: str, chunks: List[Dict[str, str]], vectors) -> str:
    """向量相似度检索。"""
    q_vec = _embed([query])[0]

    scored = []
    for chunk, vec in zip(chunks, vectors):
        sim = _cosine(q_vec, vec)
        if sim >= SIM_THRESHOLD:
            scored.append((sim, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:MAX_RESULTS]

    if not top:
        return "（知识库中未找到与您问题匹配的内容）"

    lines = [f"从平台文档中检索到 {len(top)} 条相关信息：\n"]
    for i, (sim, chunk) in enumerate(top, 1):
        lines.append(f"### [{i}] {chunk['title']}")
        lines.append(f"（来源: {chunk['file']}，相似度: {sim:.3f}）\n")
        lines.append(chunk["content"])
        lines.append("")

    return "\n".join(lines)


# =========================================================================
# 关键词检索降级（embedding 不可用时的兜底）
# =========================================================================


def _extract_keywords(text: str) -> List[str]:
    """从文本中提取中文和英文关键词（保留原有实现作为降级方案）。"""
    keywords = []

    chinese_chars = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    keywords.extend(chinese_chars)

    stopwords = {
        "the", "and", "for", "you", "that", "this", "with", "from", "have",
        "are", "not", "but", "can", "all", "was", "has", "been", "will",
        "your", "its", "also", "when", "which", "what", "how", "who",
    }
    english_words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    keywords.extend(w for w in english_words if w not in stopwords)

    return keywords


def _search_keyword_fallback(query: str, chunks: List[Dict[str, str]]) -> str:
    """关键词匹配降级检索（原有算法）。"""
    query_kw = _extract_keywords(query)
    query_lower = query.lower()

    if not query_kw:
        return "（查询无法提取有效关键词）"

    scored = []
    for chunk in chunks:
        content_lower = chunk["content"].lower()
        title_lower = chunk["title"].lower()

        keyword_hits = sum(1 for kw in query_kw if kw.lower() in content_lower)
        title_hits = sum(2 for kw in query_kw if kw.lower() in title_lower)

        phrase_bonus = 0
        for phrase in re.findall(r"[\u4e00-\u9fff]{4,}", query):
            if phrase in chunk["content"]:
                phrase_bonus += 3

        score = keyword_hits + title_hits + phrase_bonus
        if score > 0:
            scored.append((score, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:MAX_RESULTS]

    if not top:
        return "（知识库中未找到与您问题匹配的内容）"

    lines = [f"从平台文档中检索到 {len(top)} 条相关信息：\n"]
    for i, (score, chunk) in enumerate(top, 1):
        lines.append(f"### [{i}] {chunk['title']}")
        lines.append(f"（来源: {chunk['file']}，相关度: {score}）\n")
        lines.append(chunk["content"])
        lines.append("")

    return "\n".join(lines)


# =========================================================================
# 热加载 / 重建索引
# =========================================================================


def reload():
    """清除向量缓存，强制下次检索重新向量化。"""
    if CHUNKS_CACHE.exists():
        CHUNKS_CACHE.unlink()
    if VECTORS_CACHE.exists():
        VECTORS_CACHE.unlink()
    logger.info("向量缓存已清除，下次检索将重新构建索引")


def rebuild_index(docs_dir: Path = DOCS_DIR):
    """强制重建向量索引（文档更新后调用）。"""
    chunks = _load_all_docs(docs_dir)
    _build_index(chunks, force=True)
    logger.info("向量索引重建完成")


# =========================================================================
# __main__ 测试
# =========================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("knowledge_base.py 测试（向量检索）")
    print("=" * 60)

    test_queries = [
        "如何提交作业？",
        "作业一直排队怎么办",
        "conda 环境怎么配置",
        "OOM 错误怎么解决",
        "GPU 怎么申请",
    ]

    for q in test_queries:
        print(f"\n查询: {q}")
        print("-" * 40)
        result = search(q)
        # 只打印前 300 字符预览
        print(result[:300] + "..." if len(result) > 300 else result)
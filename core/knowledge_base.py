#!/usr/bin/env python3
"""
knowledge_base.py — 平台知识库检索模块。

从 docs/docs-main/docs/ 下读取所有 .md 文档，按标题分块，
用关键词匹配 + TF-IDF 相似度检索最相关的文档片段。

不需要外部依赖，纯 Python 标准库实现。
"""

import os
import re
import json
import logging
from typing import List, Dict, Tuple, Optional
from pathlib import Path

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
# 关键词提取
# =========================================================================


def _extract_keywords(text: str) -> List[str]:
    """
    从文本中提取中文和英文关键词。

    中文：按常见分隔符切分，取长度 >= 2 的词。
    英文：取长度 >= 3 的单词。
    """
    keywords = []

    # 中文关键词（按标点/空白切分，取 2-6 字词）
    chinese_chars = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    keywords.extend(chinese_chars)

    # 英文关键词（取 3+ 字母的单词，排除常见停用词）
    stopwords = {
        "the", "and", "for", "you", "that", "this", "with", "from", "have",
        "are", "not", "but", "can", "all", "was", "has", "been", "will",
        "your", "its", "also", "when", "which", "what", "how", "who",
    }
    english_words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    keywords.extend(w for w in english_words if w not in stopwords)

    # 额外：加入原始查询中的 2-4 字中文短语（作为短语级关键词）
    # 这些短语在匹配时权重更高
    extra_phrases = re.findall(r"[\u4e00-\u9fff]{2,4}", text)
    # 去重但保留顺序
    seen = set()
    for p in extra_phrases:
        if p not in seen:
            seen.add(p)
            keywords.append(p)

    return keywords


# =========================================================================
# 检索
# =========================================================================


def search(query: str, docs_dir: Optional[Path] = None) -> str:
    """
    根据用户查询检索最相关的文档片段。

    算法：
        1. 提取查询关键词
        2. 对每个文档片段计算匹配分 = 命中关键词数 + 标题匹配加分
        3. 返回 top-K 片段

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

    query_kw = _extract_keywords(query)
    query_lower = query.lower()

    if not query_kw:
        return "（查询无法提取有效关键词）"

    # 计算每个片段的匹配分
    scored: List[Tuple[int, Dict[str, str]]] = []
    for chunk in chunks:
        content_lower = chunk["content"].lower()
        title_lower = chunk["title"].lower()

        # 关键词命中分
        keyword_hits = sum(1 for kw in query_kw if kw.lower() in content_lower)

        # 标题匹配加分（标题命中权重更高）
        title_hits = sum(2 for kw in query_kw if kw.lower() in title_lower)

        # 精确短语匹配加分
        phrase_bonus = 0
        for phrase in re.findall(r"[\u4e00-\u9fff]{4,}", query):
            if phrase in chunk["content"]:
                phrase_bonus += 3

        score = keyword_hits + title_hits + phrase_bonus

        if score > 0:
            scored.append((score, chunk))

    # 按分数降序，取 top-K
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:MAX_RESULTS]

    if not top:
        return "（知识库中未找到与您问题匹配的内容）"

    # 格式化结果
    lines = [f"从平台文档中检索到 {len(top)} 条相关信息：\n"]
    for i, (score, chunk) in enumerate(top, 1):
        lines.append(f"### [{i}] {chunk['title']}")
        lines.append(f"（来源: {chunk['file']}，相关度: {score}）\n")
        lines.append(chunk["content"])
        lines.append("")

    return "\n".join(lines)


# =========================================================================
# 热加载（支持文档更新后重新加载）
# =========================================================================

_chunks_cache: Optional[List[Dict[str, str]]] = None


def reload():
    """清除缓存，强制下次检索重新加载文档。"""
    global _chunks_cache
    _chunks_cache = None
    logger.info("知识库缓存已清除")


# =========================================================================
# __main__ 测试
# =========================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("knowledge_base.py 测试")
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
"""
上下文检索：为 PR review 构建结构化的 symbol 级上下文。

检索流程：
1. diff → changed line numbers → 查 symbols 表 → changed symbols
2. changed symbol code → embedding → 向量搜索 → related symbols
3. needs_file_context() 判断是否补充 file summary（MVP 默认 false）
4. 格式化输出结构化上下文

输出结构 ReviewContextPackage 包含：
- changed_symbols: 被修改的 symbol 列表
- related_symbols: 语义相关的 symbol 列表
- file_summaries:  文件摘要（条件性补充）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import anyio

from app.llm.embedding import embed_texts
from app.review.diff_parser import extract_changed_line_numbers
from app.review.models import FileChange
from app.storage.models import SymbolRecord
from app.storage.models import build_symbol_target_key
from app.storage.pg import IndexStorageClient
from app.storage.pg import find_symbols_by_line_range
from app.storage.pg import find_symbols_by_names
from app.storage.pg import get_file_records
from app.storage.pg import search_similar_embeddings

logger = logging.getLogger(__name__)

TOP_K_SIMILAR = 8
MAX_CONTEXT_CHARS = 6000


# ---------------------------------------------------------------------------
# 上下文包模型
# ---------------------------------------------------------------------------

@dataclass
class ReviewContextPackage:
    """单个文件变更的结构化上下文包。"""

    changed_symbols: list[SymbolRecord] = field(default_factory=list)
    related_symbols: list[SymbolRecord] = field(default_factory=list)
    file_summaries: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

async def build_context_package_for_change(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    file_change: FileChange,
) -> ReviewContextPackage:
    """为单个文件变更构建结构化上下文包。

    流程：
    1. diff line numbers → symbols 表查询 → changed_symbols
    2. 对 changed symbol 做 embedding 搜索 → related_symbols
    3. 条件判断是否补充 file summary
    """
    # Step 1: 找到被修改的 symbols
    changed_symbols = await _find_changed_symbols(
        storage_client=storage_client,
        repo_id=repo_id,
        file_change=file_change,
    )
    logger.debug(
        f"[context] {file_change.path}: "
        f"found {len(changed_symbols)} changed symbols"
    )

    # Step 2: 通过 embedding 搜索相关 symbols
    related_symbols = await _search_related_symbols(
        storage_client=storage_client,
        embedding_api_base=embedding_api_base,
        repo_id=repo_id,
        changed_symbols=changed_symbols,
        exclude_path=file_change.path,
    )
    logger.debug(
        f"[context] {file_change.path}: "
        f"found {len(related_symbols)} related symbols"
    )

    # Step 3: 条件补充 file summary
    file_summaries: list[str] = []
    if needs_file_context(changed_symbols=changed_symbols, related_symbols=related_symbols):
        file_summaries = await _fetch_file_summaries(
            storage_client=storage_client,
            repo_id=repo_id,
            paths=[file_change.path],
        )

    return ReviewContextPackage(
        changed_symbols=changed_symbols,
        related_symbols=related_symbols,
        file_summaries=file_summaries,
    )


def needs_file_context(
    changed_symbols: list[SymbolRecord],
    related_symbols: list[SymbolRecord],
) -> bool:
    """判断是否需要补充 file 级 summary 上下文。

    MVP 阶段固定返回 False，后续可根据以下条件扩展：
    - changed_symbols 为空（无法定位到具体 symbol）
    - related_symbols 数量不足
    - 文件是新建文件
    """
    return False


def format_context_package(package: ReviewContextPackage) -> str:
    """将 ReviewContextPackage 格式化为 LLM prompt 用的文本。"""
    parts: list[str] = []

    if package.changed_symbols:
        parts.append("### Changed Symbols")
        for sym in package.changed_symbols:
            header = f"**{sym.kind} `{sym.name}`** ({sym.path}:{sym.start_line}-{sym.end_line})"
            code = _truncate(text=sym.code, max_chars=MAX_CONTEXT_CHARS)
            parts.append(header)
            parts.append(f"```\n{code}\n```")

    if package.related_symbols:
        parts.append("\n### Related Symbols")
        for sym in package.related_symbols:
            header = f"**{sym.kind} `{sym.name}`** ({sym.path}:{sym.start_line}-{sym.end_line})"
            code = _truncate(text=sym.code, max_chars=MAX_CONTEXT_CHARS)
            parts.append(header)
            parts.append(f"```\n{code}\n```")

    if package.file_summaries:
        parts.append("\n### File Summaries")
        for summary in package.file_summaries:
            parts.append(summary)

    return "\n".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

async def _find_changed_symbols(
    storage_client: IndexStorageClient,
    repo_id: str,
    file_change: FileChange,
) -> list[SymbolRecord]:
    """通过 diff 行号范围查找被修改的 symbols。"""
    line_numbers = extract_changed_line_numbers(diff=file_change.diff)
    if not line_numbers:
        return []

    start_line = min(line_numbers)
    end_line = max(line_numbers)

    symbols = await anyio.to_thread.run_sync(
        find_symbols_by_line_range,
        storage_client,
        repo_id,
        file_change.path,
        start_line,
        end_line,
    )
    return symbols


async def _search_related_symbols(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    changed_symbols: list[SymbolRecord],
    exclude_path: str,
) -> list[SymbolRecord]:
    """通过 embedding 相似度搜索找到相关的 symbols。

    两种策略合并：
    1. 向量搜索：用 changed symbol code 做 embedding 搜索
    2. 调用关系：查找 changed symbol 调用的函数
    """
    if not changed_symbols:
        return []

    related: list[SymbolRecord] = []
    seen_keys: set[str] = set()

    # 排除当前文件中已经被标记为 changed 的 symbols
    for sym in changed_symbols:
        key = build_symbol_target_key(path=sym.path, name=sym.name, start_line=sym.start_line)
        seen_keys.add(key)

    # 策略 1: 向量搜索
    # 用第一个 changed symbol 的 code 做查询（避免过多 embedding 调用）
    query_sym = changed_symbols[0]
    query_text = f"File: {query_sym.path}\n\nCode:\n{query_sym.code}"
    try:
        embedding_result = await embed_texts(
            api_base=embedding_api_base, texts=[query_text],
        )
        embed_hits = await anyio.to_thread.run_sync(
            search_similar_embeddings,
            storage_client,
            repo_id,
            "symbol",
            embedding_result[0],
            TOP_K_SIMILAR,
        )
        # 将 embedding hit 的 target_key 解析回 symbol 查询条件
        for hit in embed_hits:
            if hit.target_key in seen_keys:
                continue
            seen_keys.add(hit.target_key)
            # target_key 格式: "path::name::start_line"
            sym_records = await _resolve_symbol_from_target_key(
                storage_client=storage_client, repo_id=repo_id, target_key=hit.target_key,
            )
            related.extend(sym_records)
    except Exception as exc:
        logger.warning(f"[context] embedding search failed: {exc}")

    # 策略 2: 调用关系
    all_call_names: set[str] = set()
    for sym in changed_symbols:
        all_call_names.update(sym.calls)
    if all_call_names:
        call_symbols = await anyio.to_thread.run_sync(
            find_symbols_by_names, storage_client, repo_id, sorted(all_call_names),
        )
        for sym in call_symbols:
            key = build_symbol_target_key(path=sym.path, name=sym.name, start_line=sym.start_line)
            if key not in seen_keys:
                seen_keys.add(key)
                related.append(sym)

    return related


async def _resolve_symbol_from_target_key(
    storage_client: IndexStorageClient,
    repo_id: str,
    target_key: str,
) -> list[SymbolRecord]:
    """从 embedding target_key 解析出 symbol 并查询。

    target_key 格式: "path::name::start_line"
    """
    parts = target_key.split("::")
    if len(parts) < 3:
        logger.warning(f"Invalid target_key format: {target_key}")
        return []
    path = parts[0]
    name = parts[1]
    try:
        start_line = int(parts[2])
    except ValueError:
        logger.warning(f"Invalid start_line in target_key: {target_key}")
        return []

    symbols = await anyio.to_thread.run_sync(
        find_symbols_by_line_range,
        storage_client,
        repo_id,
        path,
        start_line,
        start_line,
    )
    # 精确匹配 name
    return [s for s in symbols if s.name == name]


async def _fetch_file_summaries(
    storage_client: IndexStorageClient,
    repo_id: str,
    paths: list[str],
) -> list[str]:
    """获取文件级 summary material。"""
    records = await anyio.to_thread.run_sync(
        get_file_records, storage_client, repo_id, paths,
    )
    return [r.summary_material for r in records if r.summary_material]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int) -> str:
    """截断过长文本。"""
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...TRUNCATED..."

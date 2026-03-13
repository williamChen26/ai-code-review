"""
上下文检索：为 PR review 构建结构化的 symbol 级上下文。

检索流程：
1. diff → changed line numbers → 查 symbols 表 → changed symbols
2. changed symbol code → embedding → 向量搜索 → related symbols
3. 组装 FileReviewContext（review_target + context_package + decision_trace）

输出结构 FileReviewContext 包含三层：
- review_target: 审查目标（文件、语言、变更类型）
- context_package: 上下文包（diff + changed/related symbols + 可扩展 file/module 字段）
- decision_trace: 决策追踪（上下文构建的决策记录，用于调试）
"""

from __future__ import annotations

import logging

import anyio

from app.indexing.embed_utils import build_embedding_text
from app.llm.embedding import embed_texts
from app.review.diff_parser import extract_changed_line_numbers
from app.review.models import ContextDecisionTrace
from app.review.models import ContextPackage
from app.review.models import FileChange
from app.review.models import FileReviewContext
from app.review.models import ReviewTarget
from app.review.models import SymbolContext
from app.storage.models import SymbolRecord
from app.storage.models import build_symbol_target_key
from app.storage.pg import IndexStorageClient
from app.storage.pg import find_symbols_by_line_range
from app.storage.pg import find_symbols_by_names
from app.storage.pg import search_similar_embeddings

logger = logging.getLogger(__name__)

TOP_K_SIMILAR = 8
MAX_CONTEXT_CHARS = 6000


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

async def build_file_review_context(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    file_change: FileChange,
) -> FileReviewContext:
    """为单个文件变更构建完整的 FileReviewContext。

    流程：
    1. diff line numbers → symbols 表查询 → changed_symbols
    2. 对 changed symbol 做 embedding 搜索 → related_symbols
    3. 组装 review_target + context_package + decision_trace
    """
    reasons: list[str] = []

    # Step 1: 找到被修改的 symbols
    changed_records = await _find_changed_symbols(
        storage_client=storage_client,
        repo_id=repo_id,
        file_change=file_change,
    )
    if not changed_records:
        reasons.append("no_changed_symbols_in_diff")
    logger.debug(
        f"[context] {file_change.path}: "
        f"found {len(changed_records)} changed symbols"
    )

    # Step 2: 通过 embedding 搜索相关 symbols
    related_records = await _search_related_symbols(
        storage_client=storage_client,
        embedding_api_base=embedding_api_base,
        repo_id=repo_id,
        changed_symbols=changed_records,
        exclude_path=file_change.path,
    )
    if not related_records:
        reasons.append("no_related_symbols_found")
    logger.debug(
        f"[context] {file_change.path}: "
        f"found {len(related_records)} related symbols"
    )

    # Step 3: file/module 级上下文（本期不提供，保留扩展点）
    reasons.append("file_context_deferred")

    if file_change.is_new_file:
        reasons.append("new_file_no_base_context")
    if file_change.is_deleted_file:
        reasons.append("deleted_file_limited_context")

    return FileReviewContext(
        review_target=ReviewTarget(
            file=file_change.path,
            language=file_change.language,
            is_new_file=file_change.is_new_file,
            is_deleted_file=file_change.is_deleted_file,
            is_renamed_file=file_change.is_renamed_file,
        ),
        context_package=ContextPackage(
            diff=file_change.diff,
            changed_symbols=[_to_symbol_context(s) for s in changed_records],
            related_symbols=[_to_symbol_context(s) for s in related_records],
        ),
        decision_trace=ContextDecisionTrace(
            has_changed_symbols=bool(changed_records),
            has_related_symbols=bool(related_records),
            added_file_summary=False,
            added_file_excerpt=False,
            reasons=reasons,
        ),
    )


def format_review_context(context: FileReviewContext) -> str:
    """将 FileReviewContext 的上下文部分格式化为 LLM prompt 用的文本。

    只格式化 context_package 中的 symbol 信息和可扩展字段，
    diff 由 prompt builder 单独处理（利用 recency bias 放在 prompt 末尾）。
    """
    parts: list[str] = []
    pkg = context.context_package

    if pkg.changed_symbols:
        parts.append("### Changed Symbols")
        parts.append("以下 symbol 在本次 diff 中被直接修改：")
        for sym in pkg.changed_symbols:
            header = f"**{sym.kind} `{sym.name}`** ({sym.file}:{sym.start_line}-{sym.end_line})"
            code = _truncate(text=sym.code, max_chars=MAX_CONTEXT_CHARS)
            parts.append(header)
            parts.append(f"```\n{code}\n```")

    if pkg.related_symbols:
        parts.append("\n### Related Symbols")
        parts.append("以下 symbol 与被修改代码语义相关或存在调用关系：")
        for sym in pkg.related_symbols:
            header = f"**{sym.kind} `{sym.name}`** ({sym.file}:{sym.start_line}-{sym.end_line})"
            code = _truncate(text=sym.code, max_chars=MAX_CONTEXT_CHARS)
            parts.append(header)
            parts.append(f"```\n{code}\n```")

    if pkg.file_summary:
        parts.append(f"\n### File Summary\n{pkg.file_summary}")

    if pkg.module_summary:
        parts.append(f"\n### Module Summary\n{pkg.module_summary}")

    return "\n".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

def _to_symbol_context(record: SymbolRecord) -> SymbolContext:
    """SymbolRecord（存储层） → SymbolContext（审查层）。"""
    return SymbolContext(
        name=record.name,
        kind=record.kind,
        file=record.path,
        start_line=record.start_line,
        end_line=record.end_line,
        code=record.code,
    )


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

    for sym in changed_symbols:
        key = build_symbol_target_key(path=sym.path, name=sym.name, start_line=sym.start_line)
        seen_keys.add(key)

    # 策略 1: 向量搜索
    query_sym = changed_symbols[0]
    query_text = build_embedding_text(path=query_sym.path, code=query_sym.code)
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
        for hit in embed_hits:
            if hit.target_key in seen_keys:
                continue
            seen_keys.add(hit.target_key)
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
    return [s for s in symbols if s.name == name]


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

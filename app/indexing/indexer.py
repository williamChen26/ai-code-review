"""
索引构建器。

职责：
- 全量索引（index_repo_full）：扫描 → 分 chunk 解析 → 写 files/symbols → embedding → 写 embeddings
- 增量索引（index_repo_incremental）：只处理变更/删除的文件
- 首次索引保障（ensure_initial_index）

三表写入流程（每个 chunk 独立完成）：
1. 对每个文件做 AST 解析，得到 ParsedFile（symbols + imports + calls + summary_material）
2. 写 files 表（文件级元数据，含 summary_material 供后续扩展）
3. 写 symbols 表（symbol 级记录）
4. 生成 symbol embedding → 写 embeddings 表

Embedding text 格式：
- symbol: "File: {path}\\n\\nCode:\\n{code}"

性能设计：
- 分 chunk 处理，每个 chunk 独立完成全流程后释放内存，峰值内存恒定
- 文件读取和 AST 解析通过 anyio.to_thread.run_sync 在线程池执行，不阻塞事件循环
- 全量索引使用 upsert + 尾部清理孤立记录，中途失败不会丢失已完成数据
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence

import anyio

from app.indexing.embed_utils import build_embedding_text
from app.indexing.file_scanner import scan_repo_files
from app.indexing.parser import ParsedFile
from app.indexing.parser import compute_checksum
from app.indexing.parser import parse_file
from app.llm.embedding import embed_texts
from app.review.context import infer_language_from_path
from app.storage.models import EmbeddingRecord
from app.storage.models import FileRecord
from app.storage.models import SymbolRecord
from app.storage.models import build_symbol_target_key
from app.storage.pg import IndexStorageClient
from app.storage.pg import delete_all_by_repo
from app.storage.pg import delete_embeddings_by_paths
from app.storage.pg import delete_files_by_paths
from app.storage.pg import delete_stale_files
from app.storage.pg import delete_symbols_by_paths
from app.storage.pg import list_indexed_file_paths
from app.storage.pg import upsert_embeddings
from app.storage.pg import upsert_files
from app.storage.pg import upsert_symbols

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_000_000
ALLOWED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx"}
CHUNK_SIZE = 50


def build_repo_id(provider: str, repo_key: str) -> str:
    """构建 repo_id，格式: provider:repo_key。"""
    if not provider or not repo_key:
        raise ValueError("provider and repo_key are required")
    return f"{provider}:{repo_key}"


# ---------------------------------------------------------------------------
# 全量索引
# ---------------------------------------------------------------------------

async def index_repo_full(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    repo_dir: str,
) -> None:
    """全量索引：扫描 → 分 chunk 解析/写入 → 清理孤立记录。

    不再先删旧数据，而是 upsert 覆盖 + 尾部清理，保证中途失败不丢数据。
    """
    t0 = time.monotonic()
    logger.info(f"[index_repo_full] 开始全量索引: repo_id={repo_id}, repo_dir={repo_dir}")

    files = scan_repo_files(
        repo_dir=repo_dir,
        allowed_extensions=ALLOWED_EXTENSIONS,
        max_bytes=MAX_FILE_BYTES,
    )
    relative_paths = [os.path.relpath(path, repo_dir) for path in files]
    logger.info(f"[index_repo_full] 扫描到 {len(relative_paths)} 个文件，chunk_size={CHUNK_SIZE}")

    chunks = _split_chunks(relative_paths, CHUNK_SIZE)
    for i, chunk in enumerate[list[str]](chunks):
        chunk_t0 = time.monotonic()
        logger.info(
            f"[index_repo_full] chunk {i + 1}/{len(chunks)}: "
            f"{len(chunk)} 个文件"
        )
        await _index_paths(
            storage_client=storage_client,
            embedding_api_base=embedding_api_base,
            repo_id=repo_id,
            repo_dir=repo_dir,
            paths=chunk,
        )
        chunk_elapsed = time.monotonic() - chunk_t0
        logger.info(
            f"[index_repo_full] chunk {i + 1}/{len(chunks)} 完成, "
            f"耗时 {chunk_elapsed:.1f}s"
        )

    await anyio.to_thread.run_sync(
        delete_stale_files, storage_client, repo_id, relative_paths,
    )

    elapsed = time.monotonic() - t0
    logger.info(
        f"[index_repo_full] 全量索引完成: repo_id={repo_id}, "
        f"文件数={len(relative_paths)}, 总耗时={elapsed:.1f}s"
    )


# ---------------------------------------------------------------------------
# 增量索引
# ---------------------------------------------------------------------------

async def index_repo_incremental(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    repo_dir: str,
    changed_paths: Sequence[str],
    deleted_paths: Sequence[str],
) -> None:
    """增量索引：只处理变更和删除的文件，不重算全仓。"""
    logger.info(
        f"[index_repo_incremental] repo_id={repo_id}, "
        f"changed={len(changed_paths)}, deleted={len(deleted_paths)}"
    )

    if deleted_paths:
        path_list = list(deleted_paths)
        await anyio.to_thread.run_sync(delete_embeddings_by_paths, storage_client, repo_id, path_list)
        await anyio.to_thread.run_sync(delete_symbols_by_paths, storage_client, repo_id, path_list)
        await anyio.to_thread.run_sync(delete_files_by_paths, storage_client, repo_id, path_list)
        logger.info(f"[index_repo_incremental] 已删除 {len(path_list)} 个文件的索引")

    if not changed_paths:
        return

    chunks = _split_chunks(list(changed_paths), CHUNK_SIZE)
    for i, chunk in enumerate(chunks):
        logger.info(
            f"[index_repo_incremental] chunk {i + 1}/{len(chunks)}: "
            f"{len(chunk)} 个文件"
        )
        await _index_paths(
            storage_client=storage_client,
            embedding_api_base=embedding_api_base,
            repo_id=repo_id,
            repo_dir=repo_dir,
            paths=chunk,
        )
    logger.info("[index_repo_incremental] 增量索引完成")


# ---------------------------------------------------------------------------
# 首次索引保障
# ---------------------------------------------------------------------------

async def ensure_initial_index(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    repo_dir: str,
) -> bool:
    """若该 repo_id 尚无索引，则执行全量构建；已有则跳过。

    返回：是否执行了构建。
    """
    indexed = await anyio.to_thread.run_sync(list_indexed_file_paths, storage_client, repo_id)
    if indexed:
        logger.debug(f"[ensure_initial_index] 已有索引，跳过: repo_id={repo_id}")
        return False
    await index_repo_full(
        storage_client=storage_client,
        embedding_api_base=embedding_api_base,
        repo_id=repo_id,
        repo_dir=repo_dir,
    )
    return True


# ---------------------------------------------------------------------------
# 核心索引流程（单 chunk）
# ---------------------------------------------------------------------------

async def _index_paths(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    repo_dir: str,
    paths: Sequence[str],
) -> None:
    """对单个 chunk 的路径列表执行：读取 → AST 解析 → 写 files/symbols → embedding → 写 embeddings。

    每个 chunk 独立完成全流程，完成后列表被 GC，内存恒定。
    """
    file_records: list[FileRecord] = []
    symbol_records: list[SymbolRecord] = []
    symbol_embed_texts: list[str] = []
    symbol_embed_keys: list[tuple[str, str]] = []

    for path in paths:
        full_path = os.path.join(repo_dir, path)
        if not _is_indexable_file(full_path=full_path, rel_path=path):
            continue

        content = await anyio.to_thread.run_sync(_read_text_file, full_path)
        checksum = compute_checksum(content)
        language = infer_language_from_path(path=path)

        parsed: ParsedFile = await anyio.to_thread.run_sync(
            _parse_file_sync, path, content, language,
        )

        file_record = FileRecord(
            repo_id=repo_id,
            path=path,
            language=language,
            checksum=checksum,
            summary_material=parsed.summary_material,
        )
        file_records.append(file_record)

        for sym in parsed.symbols:
            sym_record = SymbolRecord(
                repo_id=repo_id,
                path=path,
                name=sym.name,
                kind=sym.kind,
                start_line=sym.start_line,
                end_line=sym.end_line,
                code=sym.code,
                checksum=compute_checksum(sym.code),
                imports=parsed.imports,
                calls=sym.calls,
            )
            symbol_records.append(sym_record)

            embed_text = build_embedding_text(path=path, code=sym.code)
            target_key = build_symbol_target_key(
                path=path, name=sym.name, start_line=sym.start_line,
            )
            symbol_embed_texts.append(embed_text)
            symbol_embed_keys.append(("symbol", target_key))

    if file_records:
        await anyio.to_thread.run_sync(upsert_files, storage_client, file_records)
        logger.debug(f"写入 {len(file_records)} 条 file records")
    if symbol_records:
        await anyio.to_thread.run_sync(upsert_symbols, storage_client, symbol_records)
        logger.debug(f"写入 {len(symbol_records)} 条 symbol records")

    if symbol_embed_texts:
        logger.info(f"生成 {len(symbol_embed_texts)} 条 symbol embeddings...")
        symbol_vectors = await embed_texts(
            api_base=embedding_api_base, texts=symbol_embed_texts,
        )
        embed_records = [
            EmbeddingRecord(
                repo_id=repo_id,
                target_type=key[0],
                target_key=key[1],
                embedding=vec,
            )
            for key, vec in zip(symbol_embed_keys, symbol_vectors, strict=True)
        ]
        await anyio.to_thread.run_sync(upsert_embeddings, storage_client, embed_records)
        logger.debug(f"写入 {len(embed_records)} 条 symbol embedding records")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _split_chunks(items: Sequence[str], chunk_size: int) -> list[list[str]]:
    """将列表按 chunk_size 切分为多个子列表。"""
    return [
        list(items[i : i + chunk_size])
        for i in range(0, len(items), chunk_size)
    ]


def _parse_file_sync(path: str, content: str, language: str) -> ParsedFile:
    """同步包装 parse_file，用于 anyio.to_thread.run_sync。"""
    return parse_file(path=path, content=content, language=language)


def _is_indexable_file(full_path: str, rel_path: str) -> bool:
    """判断文件是否应被索引。"""
    if not os.path.exists(full_path):
        return False
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False
    try:
        size = os.path.getsize(full_path)
    except OSError:
        return False
    if size > MAX_FILE_BYTES:
        return False
    return True


def _read_text_file(path: str) -> str:
    """读取文本文件内容。"""
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()

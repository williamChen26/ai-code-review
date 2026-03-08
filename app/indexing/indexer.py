"""
索引构建器。

职责：
- 全量索引（index_repo_full）：扫描 → AST 解析 → 写 files/symbols → embedding → 写 embeddings
- 增量索引（index_repo_incremental）：只处理变更/删除的文件
- 首次索引保障（ensure_initial_index）

三表写入流程：
1. 对每个文件做 AST 解析，得到 ParsedFile（symbols + imports + calls + summary_material）
2. 写 files 表（文件级元数据）
3. 写 symbols 表（symbol 级记录）
4. 生成 symbol embedding + file summary embedding → 写 embeddings 表

Embedding text 格式：
- symbol: "File: {path}\\n\\nCode:\\n{code}"
- file:   summary_material（imports + symbol signatures）
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

import anyio

from app.indexing.file_scanner import scan_repo_files
from app.indexing.parser import ParsedFile
from app.indexing.parser import compute_checksum
from app.indexing.parser import parse_file
from app.llm.embedding import embed_texts
from app.review.context import infer_language_from_path
from app.storage.models import EmbeddingRecord
from app.storage.models import FileRecord
from app.storage.models import SymbolRecord
from app.storage.models import build_file_target_key
from app.storage.models import build_symbol_target_key
from app.storage.pg import IndexStorageClient
from app.storage.pg import delete_all_by_repo
from app.storage.pg import delete_embeddings_by_paths
from app.storage.pg import delete_files_by_paths
from app.storage.pg import delete_symbols_by_paths
from app.storage.pg import list_indexed_file_paths
from app.storage.pg import upsert_embeddings
from app.storage.pg import upsert_files
from app.storage.pg import upsert_symbols

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_000_000
ALLOWED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx"}


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
    """全量索引：清空旧数据 → 扫描 → 解析 → embedding → 写库。"""
    logger.info(f"[index_repo_full] 开始全量索引: repo_id={repo_id}, repo_dir={repo_dir}")

    # 清空旧数据（保证幂等）
    await anyio.to_thread.run_sync(delete_all_by_repo, storage_client, repo_id)

    files = scan_repo_files(
        repo_dir=repo_dir,
        allowed_extensions=ALLOWED_EXTENSIONS,
        max_bytes=MAX_FILE_BYTES,
    )
    relative_paths = [os.path.relpath(path, repo_dir) for path in files]
    logger.info(f"[index_repo_full] 扫描到 {len(relative_paths)} 个文件")

    await _index_paths(
        storage_client=storage_client,
        embedding_api_base=embedding_api_base,
        repo_id=repo_id,
        repo_dir=repo_dir,
        paths=relative_paths,
    )
    logger.info(f"[index_repo_full] 全量索引完成: repo_id={repo_id}")


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

    await _index_paths(
        storage_client=storage_client,
        embedding_api_base=embedding_api_base,
        repo_id=repo_id,
        repo_dir=repo_dir,
        paths=changed_paths,
    )
    logger.info(f"[index_repo_incremental] 增量索引完成")


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
# 核心索引流程
# ---------------------------------------------------------------------------

async def _index_paths(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    repo_dir: str,
    paths: Sequence[str],
) -> None:
    """对指定路径列表执行：读取 → AST 解析 → 写 files/symbols → embedding → 写 embeddings。"""
    file_records: list[FileRecord] = []
    symbol_records: list[SymbolRecord] = []
    # 收集需要做 embedding 的文本及对应的 target 信息
    symbol_embed_texts: list[str] = []
    symbol_embed_keys: list[tuple[str, str]] = []  # (target_type, target_key)
    file_embed_texts: list[str] = []
    file_embed_keys: list[tuple[str, str]] = []

    for path in paths:
        full_path = os.path.join(repo_dir, path)
        if not _is_indexable_file(full_path=full_path, rel_path=path):
            continue

        content = _read_text_file(full_path)
        checksum = compute_checksum(content)
        language = infer_language_from_path(path=path)

        # Step 1: AST 解析
        parsed = parse_file(path=path, content=content, language=language)

        # Step 2: 构建 FileRecord
        file_record = FileRecord(
            repo_id=repo_id,
            path=path,
            language=language,
            checksum=checksum,
            summary_material=parsed.summary_material,
        )
        file_records.append(file_record)

        # Step 3: 构建 SymbolRecords
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

            # 收集 symbol embedding 文本
            embed_text = _build_symbol_embedding_text(path=path, code=sym.code)
            target_key = build_symbol_target_key(
                path=path, name=sym.name, start_line=sym.start_line,
            )
            symbol_embed_texts.append(embed_text)
            symbol_embed_keys.append(("symbol", target_key))

        # 收集 file summary embedding 文本
        if parsed.summary_material:
            file_embed_texts.append(parsed.summary_material)
            file_embed_keys.append(("file", build_file_target_key(path=path)))

    # Step 4: 批量写 files 和 symbols
    if file_records:
        await anyio.to_thread.run_sync(upsert_files, storage_client, file_records)
        logger.debug(f"写入 {len(file_records)} 条 file records")
    if symbol_records:
        await anyio.to_thread.run_sync(upsert_symbols, storage_client, symbol_records)
        logger.debug(f"写入 {len(symbol_records)} 条 symbol records")

    # Step 5: 生成 embedding 并写入
    # 5a: symbol embeddings（主索引，必须有）
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

    # 5b: file summary embeddings（辅助索引）
    if file_embed_texts:
        logger.info(f"生成 {len(file_embed_texts)} 条 file summary embeddings...")
        file_vectors = await embed_texts(
            api_base=embedding_api_base, texts=file_embed_texts,
        )
        file_embed_records = [
            EmbeddingRecord(
                repo_id=repo_id,
                target_type=key[0],
                target_key=key[1],
                embedding=vec,
            )
            for key, vec in zip(file_embed_keys, file_vectors, strict=True)
        ]
        await anyio.to_thread.run_sync(upsert_embeddings, storage_client, file_embed_records)
        logger.debug(f"写入 {len(file_embed_records)} 条 file embedding records")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _build_symbol_embedding_text(path: str, code: str) -> str:
    """构建 symbol embedding 的输入文本。

    格式约定（与 context_retrieval 中的查询格式一致）：
    File: {path}

    Code:
    {code}
    """
    return f"File: {path}\n\nCode:\n{code}"


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

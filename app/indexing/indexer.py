"""
索引构建器。

职责：
- 首次全量索引（ensure_initial_index / index_repo_full）
- 增量索引（index_repo_incremental）
- 对每个文件：扫描 → tree-sitter 切块 → embedding → 写入数据库

Embedding 调用已从 LLM Client 剥离，直接使用 app.llm.embedding 模块（litellm SDK）。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence

import anyio

from app.indexing.chunker import chunk_file
from app.indexing.file_scanner import scan_repo_files
from app.llm.embedding import embed_texts
from app.review.context import infer_language_from_path
from app.storage.models import FileIndexEntry
from app.storage.pg import IndexStorageClient
from app.storage.pg import delete_code_chunks
from app.storage.pg import delete_file_index_entries
from app.storage.pg import list_indexed_paths
from app.storage.pg import replace_code_chunks
from app.storage.pg import upsert_file_index_entries

MAX_FILE_BYTES = 1_000_000
ALLOWED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".rb", ".php", ".rs", ".sql"}


def build_repo_id(provider: str, repo_key: str) -> str:
    if not provider or not repo_key:
        raise ValueError("provider and repo_key are required")
    return f"{provider}:{repo_key}"


async def index_repo_full(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    repo_dir: str,
) -> None:
    """全量索引：扫描仓库所有符合条件的文件，切块 + embedding + 写库。"""
    files = scan_repo_files(repo_dir=repo_dir, allowed_extensions=ALLOWED_EXTENSIONS, max_bytes=MAX_FILE_BYTES)
    relative_paths = [os.path.relpath(path, repo_dir) for path in files]
    await _index_paths(
        storage_client=storage_client,
        embedding_api_base=embedding_api_base,
        repo_id=repo_id,
        repo_dir=repo_dir,
        paths=relative_paths,
    )


async def index_repo_incremental(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    repo_dir: str,
    changed_paths: Sequence[str],
    deleted_paths: Sequence[str],
) -> None:
    """增量索引：只处理变更和删除的文件，不重算全仓。"""
    if deleted_paths:
        await anyio.to_thread.run_sync(delete_file_index_entries, storage_client, repo_id, list(deleted_paths))
        await anyio.to_thread.run_sync(delete_code_chunks, storage_client, repo_id, list(deleted_paths))
    if not changed_paths:
        return
    await _index_paths(
        storage_client=storage_client,
        embedding_api_base=embedding_api_base,
        repo_id=repo_id,
        repo_dir=repo_dir,
        paths=changed_paths,
    )


async def ensure_initial_index(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    repo_dir: str,
) -> bool:
    """若该 repo_id 尚无索引，则执行全量构建；已有则跳过，返回是否执行了构建。"""
    indexed = await anyio.to_thread.run_sync(list_indexed_paths, storage_client, repo_id)
    if indexed:
        return False
    await index_repo_full(
        storage_client=storage_client,
        embedding_api_base=embedding_api_base,
        repo_id=repo_id,
        repo_dir=repo_dir,
    )
    return True


async def _index_paths(
    storage_client: IndexStorageClient,
    embedding_api_base: str,
    repo_id: str,
    repo_dir: str,
    paths: Sequence[str],
) -> None:
    """对指定路径列表执行：读取 → 切块 → embedding → 写库。"""
    entries: list[FileIndexEntry] = []
    for path in paths:
        full_path = os.path.join(repo_dir, path)
        if not os.path.exists(full_path):
            continue
        if os.path.splitext(path)[1].lower() not in ALLOWED_EXTENSIONS:
            continue
        try:
            size = os.path.getsize(full_path)
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            continue
        content = _read_text_file(full_path)
        checksum = _sha256(content)
        entries.append(
            FileIndexEntry(
                repo_id=repo_id,
                path=path,
                language=infer_language_from_path(path=path),
                checksum=checksum,
            )
        )
        chunks = chunk_file(repo_id=repo_id, path=path, content=content)
        # 直接调用 litellm embedding，不再经过 LLM Client
        embeddings = await embed_texts(
            api_base=embedding_api_base,
            texts=[c.content for c in chunks],
        )
        await anyio.to_thread.run_sync(replace_code_chunks, storage_client, repo_id, path, chunks, embeddings)
    if entries:
        await anyio.to_thread.run_sync(upsert_file_index_entries, storage_client, entries)


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

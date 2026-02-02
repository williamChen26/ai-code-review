from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence

import anyio

from app.indexing.chunker import chunk_file
from app.indexing.file_scanner import scan_repo_files
from app.review.context import infer_language_from_path
from app.storage.models import FileIndexEntry
from app.storage.pg import IndexStorageClient
from app.storage.pg import delete_code_chunks
from app.storage.pg import delete_file_index_entries
from app.storage.pg import list_indexed_paths
from app.storage.pg import replace_code_chunks
from app.storage.pg import upsert_file_index_entries
from app.llm.client import OpenAICompatLLMClient

MAX_FILE_BYTES = 1_000_000
EMBED_BATCH_SIZE = 32
ALLOWED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".rb", ".php", ".rs", ".sql"}


def build_repo_id(provider: str, repo_key: str) -> str:
    if not provider or not repo_key:
        raise ValueError("provider and repo_key are required")
    return f"{provider}:{repo_key}"


async def index_repo_full(
    storage_client: IndexStorageClient,
    llm_client: OpenAICompatLLMClient,
    embedding_model: str,
    repo_id: str,
    repo_dir: str,
) -> None:
    files = scan_repo_files(repo_dir=repo_dir, allowed_extensions=ALLOWED_EXTENSIONS, max_bytes=MAX_FILE_BYTES)
    relative_paths = [os.path.relpath(path, repo_dir) for path in files]
    await _index_paths(
        storage_client=storage_client,
        llm_client=llm_client,
        embedding_model=embedding_model,
        repo_id=repo_id,
        repo_dir=repo_dir,
        paths=relative_paths,
    )


async def index_repo_incremental(
    storage_client: IndexStorageClient,
    llm_client: OpenAICompatLLMClient,
    embedding_model: str,
    repo_id: str,
    repo_dir: str,
    changed_paths: Sequence[str],
    deleted_paths: Sequence[str],
) -> None:
    if deleted_paths:
        await anyio.to_thread.run_sync(
            delete_file_index_entries,
            storage_client=storage_client,
            repo_id=repo_id,
            paths=list(deleted_paths),
        )
        await anyio.to_thread.run_sync(
            delete_code_chunks,
            storage_client=storage_client,
            repo_id=repo_id,
            paths=list(deleted_paths),
        )
    if not changed_paths:
        return
    await _index_paths(
        storage_client=storage_client,
        llm_client=llm_client,
        embedding_model=embedding_model,
        repo_id=repo_id,
        repo_dir=repo_dir,
        paths=changed_paths,
    )


async def ensure_initial_index(
    storage_client: IndexStorageClient,
    llm_client: OpenAICompatLLMClient,
    embedding_model: str,
    repo_id: str,
    repo_dir: str,
) -> bool:
    indexed = await anyio.to_thread.run_sync(list_indexed_paths, storage_client=storage_client, repo_id=repo_id)
    if indexed:
        return False
    await index_repo_full(
        storage_client=storage_client,
        llm_client=llm_client,
        embedding_model=embedding_model,
        repo_id=repo_id,
        repo_dir=repo_dir,
    )
    return True


async def _index_paths(
    storage_client: IndexStorageClient,
    llm_client: OpenAICompatLLMClient,
    embedding_model: str,
    repo_id: str,
    repo_dir: str,
    paths: Sequence[str],
) -> None:
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
        embeddings = await _embed_chunks(
            llm_client=llm_client,
            embedding_model=embedding_model,
            chunks=[c.content for c in chunks],
        )
        await anyio.to_thread.run_sync(
            replace_code_chunks,
            storage_client=storage_client,
            repo_id=repo_id,
            path=path,
            chunks=chunks,
            embeddings=embeddings,
        )
    if entries:
        await anyio.to_thread.run_sync(
            upsert_file_index_entries,
            storage_client=storage_client,
            entries=entries,
        )


async def _embed_chunks(
    llm_client: OpenAICompatLLMClient,
    embedding_model: str,
    chunks: Sequence[str],
) -> list[list[float]]:
    if not chunks:
        raise ValueError("chunks must not be empty")
    embeddings: list[list[float]] = []
    for i in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[i : i + EMBED_BATCH_SIZE]
        batch_embeddings = await llm_client.embed_texts(model=embedding_model, texts=batch)
        embeddings.extend(batch_embeddings)
    return embeddings


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

from __future__ import annotations

import anyio

from app.llm.client import OpenAICompatLLMClient
from app.review.diff_parser import extract_changed_line_numbers
from app.review.models import FileChange
from app.storage.models import CodeChunk
from app.storage.pg import IndexStorageClient
from app.storage.pg import find_chunks_for_line_range
from app.storage.pg import search_similar_chunks

MAX_CONTEXT_CHARS = 4000
TOP_K_SIMILAR = 8


async def build_context_package_for_change(
    storage_client: IndexStorageClient,
    llm_client: OpenAICompatLLMClient,
    embedding_model: str,
    repo_id: str,
    file_change: FileChange,
) -> str:
    line_chunks = await _find_changed_line_chunks(
        storage_client=storage_client,
        repo_id=repo_id,
        file_change=file_change,
    )
    similar_chunks = await _vector_search_chunks(
        storage_client=storage_client,
        llm_client=llm_client,
        embedding_model=embedding_model,
        repo_id=repo_id,
        file_change=file_change,
    )
    merged = _merge_chunks(line_chunks=line_chunks, similar_chunks=similar_chunks)
    return _format_context(chunks=merged)


async def _find_changed_line_chunks(
    storage_client: IndexStorageClient,
    repo_id: str,
    file_change: FileChange,
) -> list[CodeChunk]:
    lines = extract_changed_line_numbers(diff=file_change.diff)
    if not lines:
        return []
    start_line = min(lines)
    end_line = max(lines)
    return await anyio.to_thread.run_sync(
        find_chunks_for_line_range,
        storage_client,
        repo_id,
        file_change.path,
        start_line,
        end_line,
    )


async def _vector_search_chunks(
    storage_client: IndexStorageClient,
    llm_client: OpenAICompatLLMClient,
    embedding_model: str,
    repo_id: str,
    file_change: FileChange,
) -> list[CodeChunk]:
    query = f"path: {file_change.path}\n{file_change.diff}"
    embedding = await llm_client.embed_texts(model=embedding_model, texts=[query])
    return await anyio.to_thread.run_sync(
        search_similar_chunks,
        storage_client,
        repo_id,
        embedding[0],
        TOP_K_SIMILAR,
    )


def _merge_chunks(line_chunks: list[CodeChunk], similar_chunks: list[CodeChunk]) -> list[CodeChunk]:
    seen: set[tuple[str, str, int, int]] = set()
    merged: list[CodeChunk] = []
    for chunk in line_chunks + similar_chunks:
        key = (chunk.path, chunk.symbol_name, chunk.start_line, chunk.end_line)
        if key in seen:
            continue
        seen.add(key)
        merged.append(chunk)
    return merged


def _format_context(chunks: list[CodeChunk]) -> str:
    if not chunks:
        return ""
    parts: list[str] = ["相关上下文："]
    for chunk in chunks:
        header = f"- {chunk.path}::{chunk.symbol_name} ({chunk.start_line}-{chunk.end_line})"
        body = _truncate(text=chunk.content, max_chars=MAX_CONTEXT_CHARS)
        parts.append(header)
        parts.append(body)
    return "\n".join(parts)


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...TRUNCATED..."

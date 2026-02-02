from __future__ import annotations

import logging
from collections.abc import Sequence

import psycopg
from pgvector.psycopg import register_vector

from app.storage.models import CodeChunk
from app.storage.models import FileIndexEntry

logger = logging.getLogger(__name__)


class IndexStorageClient:
    """Postgres + pgvector 连接器。"""

    def __init__(self, dsn: str, embedding_dim: int) -> None:
        self._dsn = dsn
        self._embedding_dim = embedding_dim

    def connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self._dsn)
        register_vector(conn)
        return conn

    @property
    def embedding_dim(self) -> int:
        return self._embedding_dim


def ensure_schema(client: IndexStorageClient) -> None:
    with client.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS file_index (
                    repo_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    language TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    PRIMARY KEY (repo_id, path)
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS code_chunks (
                    repo_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    symbol_name TEXT NOT NULL,
                    symbol_type TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    embedding VECTOR({client.embedding_dim}) NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_code_chunks_repo_path
                ON code_chunks (repo_id, path)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_code_chunks_repo_embedding
                ON code_chunks USING ivfflat (embedding vector_cosine_ops)
                """
            )
        conn.commit()


def upsert_file_index_entries(client: IndexStorageClient, entries: Sequence[FileIndexEntry]) -> None:
    if not entries:
        raise ValueError("entries must not be empty")
    with client.connect() as conn:
        with conn.cursor() as cur:
            for entry in entries:
                cur.execute(
                    """
                    INSERT INTO file_index (repo_id, path, language, checksum)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (repo_id, path)
                    DO UPDATE SET language = EXCLUDED.language, checksum = EXCLUDED.checksum
                    """,
                    (entry.repo_id, entry.path, entry.language, entry.checksum),
                )
        conn.commit()


def delete_file_index_entries(client: IndexStorageClient, repo_id: str, paths: Sequence[str]) -> None:
    if not paths:
        raise ValueError("paths must not be empty")
    with client.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM file_index WHERE repo_id = %s AND path = ANY(%s)",
                (repo_id, list(paths)),
            )
        conn.commit()


def replace_code_chunks(client: IndexStorageClient, repo_id: str, path: str, chunks: Sequence[CodeChunk], embeddings: Sequence[Sequence[float]]) -> None:
    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings length mismatch")
    with client.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM code_chunks WHERE repo_id = %s AND path = %s", (repo_id, path))
            for chunk, embedding in zip(chunks, embeddings, strict=True):
                cur.execute(
                    """
                    INSERT INTO code_chunks (
                        repo_id, path, symbol_name, symbol_type, start_line, end_line, content, checksum, embedding
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chunk.repo_id,
                        chunk.path,
                        chunk.symbol_name,
                        chunk.symbol_type,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.content,
                        chunk.checksum,
                        embedding,
                    ),
                )
        conn.commit()


def delete_code_chunks(client: IndexStorageClient, repo_id: str, paths: Sequence[str]) -> None:
    if not paths:
        raise ValueError("paths must not be empty")
    with client.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM code_chunks WHERE repo_id = %s AND path = ANY(%s)", (repo_id, list(paths)))
        conn.commit()


def search_similar_chunks(
    client: IndexStorageClient,
    repo_id: str,
    query_embedding: Sequence[float],
    limit: int,
) -> list[CodeChunk]:
    if limit <= 0:
        raise ValueError("limit must be > 0")
    with client.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT repo_id, path, symbol_name, symbol_type, start_line, end_line, content, checksum
                FROM code_chunks
                WHERE repo_id = %s
                ORDER BY embedding <-> %s
                LIMIT %s
                """,
                (repo_id, list(query_embedding), limit),
            )
            rows = cur.fetchall()
    return [
        CodeChunk(
            repo_id=row[0],
            path=row[1],
            symbol_name=row[2],
            symbol_type=row[3],
            start_line=row[4],
            end_line=row[5],
            content=row[6],
            checksum=row[7],
        )
        for row in rows
    ]


def list_indexed_paths(client: IndexStorageClient, repo_id: str) -> list[str]:
    with client.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT path FROM file_index WHERE repo_id = %s", (repo_id,))
            rows = cur.fetchall()
    return [row[0] for row in rows]


def get_file_index_entries(client: IndexStorageClient, repo_id: str, paths: Sequence[str]) -> list[FileIndexEntry]:
    if not paths:
        raise ValueError("paths must not be empty")
    with client.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo_id, path, language, checksum FROM file_index WHERE repo_id = %s AND path = ANY(%s)",
                (repo_id, list(paths)),
            )
            rows = cur.fetchall()
    return [
        FileIndexEntry(repo_id=row[0], path=row[1], language=row[2], checksum=row[3]) for row in rows
    ]


def find_chunks_for_line_range(
    client: IndexStorageClient,
    repo_id: str,
    path: str,
    start_line: int,
    end_line: int,
) -> list[CodeChunk]:
    if start_line <= 0 or end_line < start_line:
        raise ValueError("Invalid line range")
    with client.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT repo_id, path, symbol_name, symbol_type, start_line, end_line, content, checksum
                FROM code_chunks
                WHERE repo_id = %s AND path = %s AND start_line <= %s AND end_line >= %s
                """,
                (repo_id, path, end_line, start_line),
            )
            rows = cur.fetchall()
    return [
        CodeChunk(
            repo_id=row[0],
            path=row[1],
            symbol_name=row[2],
            symbol_type=row[3],
            start_line=row[4],
            end_line=row[5],
            content=row[6],
            checksum=row[7],
        )
        for row in rows
    ]


def list_all_repo_ids(client: IndexStorageClient) -> list[str]:
    with client.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT repo_id FROM file_index")
            rows = cur.fetchall()
    return [row[0] for row in rows]

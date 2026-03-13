"""
Postgres + pgvector 存储层。

三表设计：
- files:      文件级元数据（语言、校验和、摘要材料）
- symbols:    symbol 级记录（函数/类/方法，含代码、imports、calls）
- embeddings: 向量表，通过 target_type + target_key 与业务表解耦

所有写操作使用 upsert 语义，保证幂等。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import psycopg
from pgvector.psycopg import register_vector

from app.storage.models import EmbeddingRecord
from app.storage.models import FileRecord
from app.storage.models import SymbolRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class IndexStorageClient:
    """Postgres + pgvector 连接器（内部缓存连接，避免重复创建）。"""

    def __init__(self, dsn: str, prepare_threshold: int | None = None) -> None:
        self._dsn = dsn
        self.prepare_threshold = prepare_threshold
        self._conn: psycopg.Connection | None = None

    def connect(self) -> psycopg.Connection:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = psycopg.connect(self._dsn, prepare_threshold=self.prepare_threshold)
        register_vector(self._conn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
            self._conn = None


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

def ensure_schema(client: IndexStorageClient) -> None:
    """创建三表 + 索引（幂等，可重复调用）。"""
    conn = client.connect()
    with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

            # -- files 表 --
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    repo_id          TEXT NOT NULL,
                    path             TEXT NOT NULL,
                    language         TEXT NOT NULL,
                    checksum         TEXT NOT NULL,
                    summary_material TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (repo_id, path)
                )
                """
            )

            # -- symbols 表 --
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS symbols (
                    repo_id    TEXT    NOT NULL,
                    path       TEXT    NOT NULL,
                    name       TEXT    NOT NULL,
                    kind       TEXT    NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line   INTEGER NOT NULL,
                    code       TEXT    NOT NULL,
                    checksum   TEXT    NOT NULL,
                    imports    JSONB   NOT NULL DEFAULT '[]',
                    calls      JSONB   NOT NULL DEFAULT '[]'
                )
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_symbols_identity
                ON symbols (repo_id, path, name, start_line)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_symbols_repo_path
                ON symbols (repo_id, path)
                """
            )

            # -- embeddings 表 --
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    repo_id     TEXT   NOT NULL,
                    target_type TEXT   NOT NULL,
                    target_key  TEXT   NOT NULL,
                    embedding   VECTOR(1536) NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_embeddings_identity
                ON embeddings (repo_id, target_type, target_key)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_embeddings_vector
                ON embeddings USING hnsw (embedding vector_cosine_ops)
                """
            )

    conn.commit()
    logger.info("Schema ensured: files / symbols / embeddings")


# ---------------------------------------------------------------------------
# files CRUD
# ---------------------------------------------------------------------------

def upsert_files(client: IndexStorageClient, records: Sequence[FileRecord]) -> None:
    """批量 upsert 文件记录（executemany 一次提交）。"""
    if not records:
        return
    conn = client.connect()
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO files (repo_id, path, language, checksum, summary_material)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (repo_id, path)
            DO UPDATE SET language         = EXCLUDED.language,
                          checksum         = EXCLUDED.checksum,
                          summary_material = EXCLUDED.summary_material
            """,
            [
                (r.repo_id, r.path, r.language, r.checksum, r.summary_material)
                for r in records
            ],
        )
    conn.commit()


def delete_files_by_paths(
    client: IndexStorageClient, repo_id: str, paths: Sequence[str]
) -> None:
    """删除指定文件记录。"""
    if not paths:
        return
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM files WHERE repo_id = %s AND path = ANY(%s)",
            (repo_id, list(paths)),
        )
    conn.commit()


def list_indexed_file_paths(client: IndexStorageClient, repo_id: str) -> list[str]:
    """列出该 repo 已索引的所有文件路径。"""
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT path FROM files WHERE repo_id = %s", (repo_id,))
        rows = cur.fetchall()
    return [row[0] for row in rows]


def get_file_records(
    client: IndexStorageClient, repo_id: str, paths: Sequence[str]
) -> list[FileRecord]:
    """按路径批量查询文件记录。"""
    if not paths:
        return []
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo_id, path, language, checksum, summary_material
            FROM files
            WHERE repo_id = %s AND path = ANY(%s)
            """,
            (repo_id, list(paths)),
        )
        rows = cur.fetchall()
    return [
        FileRecord(
            repo_id=row[0], path=row[1], language=row[2],
            checksum=row[3], summary_material=row[4],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# symbols CRUD
# ---------------------------------------------------------------------------

def upsert_symbols(client: IndexStorageClient, records: Sequence[SymbolRecord]) -> None:
    """批量 upsert symbol 记录（executemany 一次提交）。"""
    if not records:
        return
    conn = client.connect()
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO symbols
                (repo_id, path, name, kind, start_line, end_line, code, checksum, imports, calls)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (repo_id, path, name, start_line)
            DO UPDATE SET kind      = EXCLUDED.kind,
                          end_line  = EXCLUDED.end_line,
                          code      = EXCLUDED.code,
                          checksum  = EXCLUDED.checksum,
                          imports   = EXCLUDED.imports,
                          calls     = EXCLUDED.calls
            """,
            [
                (
                    r.repo_id, r.path, r.name, r.kind,
                    r.start_line, r.end_line, r.code, r.checksum,
                    json.dumps(r.imports), json.dumps(r.calls),
                )
                for r in records
            ],
        )
    conn.commit()


def delete_symbols_by_paths(
    client: IndexStorageClient, repo_id: str, paths: Sequence[str]
) -> None:
    """删除指定路径下的所有 symbol。"""
    if not paths:
        return
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM symbols WHERE repo_id = %s AND path = ANY(%s)",
            (repo_id, list(paths)),
        )
    conn.commit()


def find_symbols_by_line_range(
    client: IndexStorageClient,
    repo_id: str,
    path: str,
    start_line: int,
    end_line: int,
) -> list[SymbolRecord]:
    """查找与指定行范围有交集的 symbol（用于 diff -> changed symbols 映射）。"""
    if start_line <= 0 or end_line < start_line:
        raise ValueError(f"Invalid line range: start_line={start_line}, end_line={end_line}")
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo_id, path, name, kind, start_line, end_line,
                   code, checksum, imports, calls
            FROM symbols
            WHERE repo_id = %s AND path = %s
              AND start_line <= %s AND end_line >= %s
            """,
            (repo_id, path, end_line, start_line),
        )
        rows = cur.fetchall()
    return [_row_to_symbol(row) for row in rows]


def find_symbols_by_names(
    client: IndexStorageClient,
    repo_id: str,
    names: Sequence[str],
) -> list[SymbolRecord]:
    """按 symbol 名称查找（用于调用关系图查询）。"""
    if not names:
        return []
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo_id, path, name, kind, start_line, end_line,
                   code, checksum, imports, calls
            FROM symbols
            WHERE repo_id = %s AND name = ANY(%s)
            """,
            (repo_id, list(names)),
        )
        rows = cur.fetchall()
    return [_row_to_symbol(row) for row in rows]


def find_symbols_by_path(
    client: IndexStorageClient,
    repo_id: str,
    path: str,
) -> list[SymbolRecord]:
    """查找指定文件中的所有 symbol。"""
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo_id, path, name, kind, start_line, end_line,
                   code, checksum, imports, calls
            FROM symbols
            WHERE repo_id = %s AND path = %s
            ORDER BY start_line
            """,
            (repo_id, path),
        )
        rows = cur.fetchall()
    return [_row_to_symbol(row) for row in rows]


def _row_to_symbol(row: tuple) -> SymbolRecord:  # type: ignore[type-arg]
    """将数据库行转换为 SymbolRecord。"""
    imports_raw = row[8]
    calls_raw = row[9]
    # JSONB 列可能已经被 psycopg 解析为 list，也可能是 str
    imports = imports_raw if isinstance(imports_raw, list) else json.loads(imports_raw)
    calls = calls_raw if isinstance(calls_raw, list) else json.loads(calls_raw)
    return SymbolRecord(
        repo_id=row[0], path=row[1], name=row[2], kind=row[3],
        start_line=row[4], end_line=row[5], code=row[6], checksum=row[7],
        imports=imports, calls=calls,
    )


# ---------------------------------------------------------------------------
# embeddings CRUD
# ---------------------------------------------------------------------------

def upsert_embeddings(
    client: IndexStorageClient, records: Sequence[EmbeddingRecord]
) -> None:
    """批量 upsert embedding 记录（executemany 一次提交）。"""
    if not records:
        return
    conn = client.connect()
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO embeddings (repo_id, target_type, target_key, embedding)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (repo_id, target_type, target_key)
            DO UPDATE SET embedding = EXCLUDED.embedding
            """,
            [
                (r.repo_id, r.target_type, r.target_key, r.embedding)
                for r in records
            ],
        )
    conn.commit()


def delete_embeddings_by_paths(
    client: IndexStorageClient,
    repo_id: str,
    paths: Sequence[str],
) -> None:
    """删除与指定路径相关的所有 embedding（symbol 和 file 类型）。

    symbol target_key 格式: "path::name::start_line"，以 path 为前缀
    file   target_key 格式: "path"，精确匹配
    """
    if not paths:
        return
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM embeddings
            WHERE repo_id = %s AND target_type = 'file' AND target_key = ANY(%s)
            """,
            (repo_id, list(paths)),
        )
        for p in paths:
            cur.execute(
                """
                DELETE FROM embeddings
                WHERE repo_id = %s AND target_type = 'symbol'
                  AND target_key LIKE %s
                """,
                (repo_id, f"{p}::%"),
            )
    conn.commit()


def search_similar_embeddings(
    client: IndexStorageClient,
    repo_id: str,
    target_type: str,
    query_embedding: Sequence[float],
    limit: int,
) -> list[EmbeddingRecord]:
    """向量相似度搜索（cosine distance）。

    - target_type: 'symbol' 或 'file'，限定搜索范围
    - 返回最相似的 limit 条记录（不含向量本身，减少传输量）
    """
    if limit <= 0:
        raise ValueError("limit must be > 0")
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo_id, target_type, target_key
            FROM embeddings
            WHERE repo_id = %s AND target_type = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (repo_id, target_type, list(query_embedding), limit),
        )
        rows = cur.fetchall()
    return [
        EmbeddingRecord(
            repo_id=row[0], target_type=row[1], target_key=row[2],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# 通用查询
# ---------------------------------------------------------------------------

def list_all_repo_ids(client: IndexStorageClient) -> list[str]:
    """列出所有已索引的 repo_id。"""
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT repo_id FROM files")
        rows = cur.fetchall()
    return [row[0] for row in rows]


def delete_all_by_repo(client: IndexStorageClient, repo_id: str) -> None:
    """删除指定 repo 的所有数据（全量重建前调用）。"""
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE repo_id = %s", (repo_id,))
        cur.execute("DELETE FROM symbols WHERE repo_id = %s", (repo_id,))
        cur.execute("DELETE FROM files WHERE repo_id = %s", (repo_id,))
    conn.commit()
    logger.info(f"Deleted all index data for repo_id={repo_id}")


def delete_stale_files(
    client: IndexStorageClient, repo_id: str, valid_paths: Sequence[str]
) -> None:
    """清理不在 valid_paths 中的孤立记录（全量索引结束后调用）。"""
    if not valid_paths:
        return
    conn = client.connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT path FROM files WHERE repo_id = %s AND path != ALL(%s)",
            (repo_id, list(valid_paths)),
        )
        stale_paths = [row[0] for row in cur.fetchall()]
    if not stale_paths:
        return
    logger.info(f"Cleaning {len(stale_paths)} stale paths for repo_id={repo_id}")
    delete_embeddings_by_paths(client=client, repo_id=repo_id, paths=stale_paths)
    delete_symbols_by_paths(client=client, repo_id=repo_id, paths=stale_paths)
    delete_files_by_paths(client=client, repo_id=repo_id, paths=stale_paths)

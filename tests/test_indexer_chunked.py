"""
测试 indexer 的分 chunk 处理、安全写入和工具函数。

可本地运行：
    pytest tests/test_indexer_chunked.py -v

不需要真实 API 或数据库。
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.indexing.indexer import (
    ALLOWED_EXTENSIONS,
    CHUNK_SIZE,
    MAX_FILE_BYTES,
    _is_indexable_file,
    _read_text_file,
    _split_chunks,
    build_repo_id,
    index_repo_full,
)


# ---------------------------------------------------------------------------
# Tests: _split_chunks
# ---------------------------------------------------------------------------

def test_split_chunks_exact() -> None:
    """长度恰好是 chunk_size 的整数倍。"""
    items = [str(i) for i in range(10)]
    chunks = _split_chunks(items, chunk_size=5)
    assert len(chunks) == 2
    assert chunks[0] == ["0", "1", "2", "3", "4"]
    assert chunks[1] == ["5", "6", "7", "8", "9"]


def test_split_chunks_remainder() -> None:
    """长度不是 chunk_size 的整数倍，最后一组较短。"""
    items = [str(i) for i in range(7)]
    chunks = _split_chunks(items, chunk_size=3)
    assert len(chunks) == 3
    assert chunks[2] == ["6"]


def test_split_chunks_empty() -> None:
    """空列表返回空。"""
    assert _split_chunks([], chunk_size=5) == []


def test_split_chunks_smaller_than_size() -> None:
    """列表比 chunk_size 短，只有一组。"""
    items = ["a", "b"]
    chunks = _split_chunks(items, chunk_size=100)
    assert len(chunks) == 1
    assert chunks[0] == ["a", "b"]


# ---------------------------------------------------------------------------
# Tests: build_repo_id
# ---------------------------------------------------------------------------

def test_build_repo_id() -> None:
    assert build_repo_id(provider="gitlab", repo_key="123") == "gitlab:123"
    assert build_repo_id(provider="github", repo_key="owner/repo") == "github:owner/repo"


def test_build_repo_id_empty_raises() -> None:
    with pytest.raises(ValueError):
        build_repo_id(provider="", repo_key="123")
    with pytest.raises(ValueError):
        build_repo_id(provider="gitlab", repo_key="")


# ---------------------------------------------------------------------------
# Tests: _is_indexable_file
# ---------------------------------------------------------------------------

def test_is_indexable_file_accepts_valid() -> None:
    """允许扩展名 + 合理大小 → True。"""
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
        f.write(b"x = 1\n")
        f.flush()
        path = f.name
    try:
        assert _is_indexable_file(full_path=path, rel_path="src/main.py")
    finally:
        os.unlink(path)


def test_is_indexable_file_rejects_wrong_extension() -> None:
    """不在 ALLOWED_EXTENSIONS 中的扩展名 → False。"""
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        f.write(b"hello")
        f.flush()
        path = f.name
    try:
        assert not _is_indexable_file(full_path=path, rel_path="README.md")
    finally:
        os.unlink(path)


def test_is_indexable_file_rejects_nonexistent() -> None:
    """文件不存在 → False。"""
    assert not _is_indexable_file(full_path="/no/such/file.py", rel_path="no.py")


def test_is_indexable_file_rejects_oversized() -> None:
    """超过 MAX_FILE_BYTES → False。"""
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
        f.write(b"x" * (MAX_FILE_BYTES + 1))
        f.flush()
        path = f.name
    try:
        assert not _is_indexable_file(full_path=path, rel_path="big.py")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Tests: _read_text_file
# ---------------------------------------------------------------------------

def test_read_text_file() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("def hello():\n    pass\n")
        path = f.name
    try:
        content = _read_text_file(path)
        assert "def hello():" in content
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Tests: index_repo_full（mock 集成测试）
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_index_repo_full_calls_chunks() -> None:
    """全量索引应分 chunk 调用 _index_paths，并在尾部清理孤立记录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(3):
            path = os.path.join(tmpdir, f"file_{i}.py")
            with open(path, "w") as f:
                f.write(f"def func_{i}():\n    pass\n")

        mock_storage = MagicMock()
        calls_to_index_paths: list[int] = []

        async def mock_index_paths(
            storage_client, embedding_api_base, repo_id, repo_dir, paths,
        ) -> None:
            calls_to_index_paths.append(len(paths))

        with patch("app.indexing.indexer._index_paths", side_effect=mock_index_paths):
            with patch("app.indexing.indexer.delete_stale_files") as mock_cleanup:
                with patch("app.indexing.indexer.CHUNK_SIZE", 2):
                    await index_repo_full(
                        storage_client=mock_storage,
                        embedding_api_base="http://fake",
                        repo_id="test:repo",
                        repo_dir=tmpdir,
                    )

        assert len(calls_to_index_paths) == 2
        assert calls_to_index_paths[0] == 2
        assert calls_to_index_paths[1] == 1
        mock_cleanup.assert_called_once()


@pytest.mark.anyio
async def test_index_repo_full_no_delete_all() -> None:
    """全量索引不再先 delete_all_by_repo。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "a.py")
        with open(path, "w") as f:
            f.write("x = 1\n")

        mock_storage = MagicMock()

        with patch("app.indexing.indexer._index_paths", new_callable=AsyncMock):
            with patch("app.indexing.indexer.delete_stale_files"):
                with patch("app.indexing.indexer.delete_all_by_repo") as mock_delete_all:
                    await index_repo_full(
                        storage_client=mock_storage,
                        embedding_api_base="http://fake",
                        repo_id="test:repo",
                        repo_dir=tmpdir,
                    )

        mock_delete_all.assert_not_called()

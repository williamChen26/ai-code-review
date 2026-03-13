"""
测试 embedding 模块的并发 + 重试 + 超时机制。

可本地运行：
    pytest tests/test_embedding_retry.py -v

不需要真实 API 或数据库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import anyio
import pytest

from app.llm.embedding import (
    EMBED_BATCH_SIZE,
    EMBED_CONCURRENCY,
    embed_texts,
    _embed_batch_with_retry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class FakeEmbeddingItem:
    embedding: list[float]


@dataclass
class FakeEmbeddingResponse:
    data: list[FakeEmbeddingItem]


def _make_fake_response(count: int, dim: int = 8) -> FakeEmbeddingResponse:
    """构造一个 fake embedding response，每个向量为 [0.1]*dim。"""
    return FakeEmbeddingResponse(
        data=[FakeEmbeddingItem(embedding=[0.1] * dim) for _ in range(count)]
    )


# ---------------------------------------------------------------------------
# Tests: embed_texts
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_embed_texts_single_batch() -> None:
    """文本数 <= EMBED_BATCH_SIZE 时，只发一个 batch。"""
    texts = [f"text_{i}" for i in range(5)]
    fake_response = _make_fake_response(count=5)

    with patch("app.llm.embedding.litellm") as mock_litellm:
        mock_litellm.aembedding = AsyncMock(return_value=fake_response)
        result = await embed_texts(api_base="http://fake", texts=texts)

    assert len(result) == 5
    assert all(len(v) == 8 for v in result)
    mock_litellm.aembedding.assert_called_once()


@pytest.mark.anyio
async def test_embed_texts_multiple_batches() -> None:
    """文本数 > EMBED_BATCH_SIZE 时，分多个 batch 并发调用。"""
    total = EMBED_BATCH_SIZE * 3 + 10
    texts = [f"text_{i}" for i in range(total)]

    call_count = 0

    async def fake_aembedding(**kwargs: Any) -> FakeEmbeddingResponse:
        nonlocal call_count
        call_count += 1
        batch_input = kwargs["input"]
        return _make_fake_response(count=len(batch_input))

    with patch("app.llm.embedding.litellm") as mock_litellm:
        mock_litellm.aembedding = fake_aembedding
        result = await embed_texts(api_base="http://fake", texts=texts)

    assert len(result) == total
    expected_batches = (total + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE
    assert call_count == expected_batches


@pytest.mark.anyio
async def test_embed_texts_concurrency_limit() -> None:
    """并发数不超过 EMBED_CONCURRENCY。"""
    total = EMBED_BATCH_SIZE * 8
    texts = [f"text_{i}" for i in range(total)]

    max_concurrent = 0
    current_concurrent = 0

    async def fake_aembedding(**kwargs: Any) -> FakeEmbeddingResponse:
        nonlocal max_concurrent, current_concurrent
        current_concurrent += 1
        if current_concurrent > max_concurrent:
            max_concurrent = current_concurrent
        await anyio.sleep(0.01)
        current_concurrent -= 1
        return _make_fake_response(count=len(kwargs["input"]))

    with patch("app.llm.embedding.litellm") as mock_litellm:
        mock_litellm.aembedding = fake_aembedding
        result = await embed_texts(api_base="http://fake", texts=texts)

    assert len(result) == total
    assert max_concurrent <= EMBED_CONCURRENCY


@pytest.mark.anyio
async def test_embed_texts_empty_raises() -> None:
    """空文本列表应抛出 ValueError。"""
    with pytest.raises(ValueError, match="texts must not be empty"):
        await embed_texts(api_base="http://fake", texts=[])


# ---------------------------------------------------------------------------
# Tests: _embed_batch_with_retry
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_retry_success_on_second_attempt() -> None:
    """第一次失败后重试成功。"""
    attempt_count = 0

    async def flaky_aembedding(**kwargs: Any) -> FakeEmbeddingResponse:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise RuntimeError("transient error")
        return _make_fake_response(count=len(kwargs["input"]))

    with patch("app.llm.embedding.litellm") as mock_litellm:
        mock_litellm.aembedding = flaky_aembedding
        # 减少 sleep 等待时间以加速测试
        with patch("app.llm.embedding.anyio") as mock_anyio:
            mock_anyio.fail_after = anyio.fail_after
            mock_anyio.sleep = AsyncMock()
            mock_anyio.Semaphore = anyio.Semaphore
            mock_anyio.create_task_group = anyio.create_task_group

            result = await _embed_batch_with_retry(
                api_base="http://fake",
                batch=["a", "b"],
                batch_index=0,
            )

    assert len(result) == 2
    assert attempt_count == 2


@pytest.mark.anyio
async def test_retry_exhausted_raises() -> None:
    """所有重试都失败后应抛出 RuntimeError。"""
    async def always_fail(**kwargs: Any) -> None:
        raise RuntimeError("persistent error")

    with patch("app.llm.embedding.litellm") as mock_litellm:
        mock_litellm.aembedding = always_fail
        with patch("app.llm.embedding.anyio") as mock_anyio:
            mock_anyio.fail_after = anyio.fail_after
            mock_anyio.sleep = AsyncMock()

            with pytest.raises(RuntimeError, match="exhausted"):
                await _embed_batch_with_retry(
                    api_base="http://fake",
                    batch=["a"],
                    batch_index=0,
                )


@pytest.mark.anyio
async def test_response_length_mismatch_raises() -> None:
    """API 返回的向量数与输入数不匹配时应抛出 RuntimeError。"""
    bad_response = _make_fake_response(count=1)

    with patch("app.llm.embedding.litellm") as mock_litellm:
        mock_litellm.aembedding = AsyncMock(return_value=bad_response)
        with patch("app.llm.embedding.anyio") as mock_anyio:
            mock_anyio.fail_after = anyio.fail_after
            mock_anyio.sleep = AsyncMock()

            with pytest.raises(RuntimeError, match="exhausted"):
                await _embed_batch_with_retry(
                    api_base="http://fake",
                    batch=["a", "b", "c"],
                    batch_index=0,
                )

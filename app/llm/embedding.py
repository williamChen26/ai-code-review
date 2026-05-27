"""
Embedding 客户端（基于 LiteLLM SDK）。

职责：
- 调用 litellm.aembedding 生成文本向量
- 与 LLM 聊天补全完全解耦，独立管理 embedding 调用

设计：
- 纯异步函数，无状态，不使用类封装
- API Key 由 litellm 从环境变量 OPENAI_API_KEY 自动读取
- 支持并发调用（Semaphore 限流）+ 指数退避重试 + 超时
- 调用失败重试耗尽后抛异常，不做静默处理
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import anyio
import litellm

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "litellm_proxy/Embedding-3-Small"
EMBED_BATCH_SIZE = 32
EMBED_CONCURRENCY = 4
EMBED_RETRY_COUNT = 3
EMBED_TIMEOUT_SECONDS = 60


async def embed_texts(
    api_base: str,
    texts: Sequence[str],
) -> list[list[float]]:
    """
    使用 LiteLLM SDK 生成文本 embedding 向量。

    参数：
    - api_base: LiteLLM Proxy 地址，例如 "http://litellm-internal.example.com/"
    - texts: 待向量化的文本列表

    返回：
    - 与 texts 等长的 embedding 向量列表

    注意：
    - API Key 由 litellm 自动从环境变量 OPENAI_API_KEY 读取，无需显式传入
    - 超过 EMBED_BATCH_SIZE 的文本会自动分批处理
    - 批次间并发执行（受 EMBED_CONCURRENCY 控制）
    - 每个批次自带重试（EMBED_RETRY_COUNT 次）和超时（EMBED_TIMEOUT_SECONDS 秒）
    """
    if not texts:
        raise ValueError("texts must not be empty")

    batches: list[list[str]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batches.append(list(texts[i : i + EMBED_BATCH_SIZE]))

    logger.info(
        f"Embedding: total={len(texts)}, batches={len(batches)}, "
        f"batch_size={EMBED_BATCH_SIZE}, concurrency={EMBED_CONCURRENCY}"
    )

    results: list[list[list[float]]] = [[] for _ in batches]
    semaphore = anyio.Semaphore(EMBED_CONCURRENCY)

    async def _process_batch(idx: int, batch: list[str]) -> None:
        async with semaphore:
            vectors = await _embed_batch_with_retry(
                api_base=api_base,
                batch=batch,
                batch_index=idx,
            )
            results[idx] = vectors

    async with anyio.create_task_group() as tg:
        for idx, batch in enumerate(batches):
            tg.start_soon(_process_batch, idx, batch)

    all_embeddings: list[list[float]] = []
    for batch_result in results:
        all_embeddings.extend(batch_result)

    if len(all_embeddings) != len(texts):
        raise RuntimeError(
            f"Embedding total mismatch: expected {len(texts)}, got {len(all_embeddings)}"
        )

    dim = len(all_embeddings[0]) if all_embeddings else 0
    logger.info(f"Embedding ok: total={len(all_embeddings)}, dim={dim}")
    return all_embeddings


async def _embed_batch_with_retry(
    api_base: str,
    batch: list[str],
    batch_index: int,
) -> list[list[float]]:
    """单个 batch 的 embedding 调用，带指数退避重试和超时。"""
    last_error: Exception | None = None

    for attempt in range(1, EMBED_RETRY_COUNT + 1):
        try:
            logger.debug(
                f"Embedding batch {batch_index}: "
                f"size={len(batch)}, attempt={attempt}/{EMBED_RETRY_COUNT}"
            )

            with anyio.fail_after(EMBED_TIMEOUT_SECONDS):
                response = await litellm.aembedding(
                    model=EMBEDDING_MODEL,
                    api_base=api_base,
                    input=batch,
                )

            batch_embeddings = [
                list(item["embedding"]) if isinstance(item, dict) else list(item.embedding)
                for item in response.data
            ]
            if len(batch_embeddings) != len(batch):
                raise RuntimeError(
                    f"Batch {batch_index} length mismatch: "
                    f"expected {len(batch)}, got {len(batch_embeddings)}"
                )

            logger.debug(f"Embedding batch {batch_index} ok: size={len(batch_embeddings)}")
            return batch_embeddings

        except Exception as exc:
            last_error = exc
            if attempt < EMBED_RETRY_COUNT:
                delay = 2 ** attempt
                logger.warning(
                    f"Embedding batch {batch_index} attempt {attempt} failed: {exc}. "
                    f"Retrying in {delay}s..."
                )
                await anyio.sleep(delay)
            else:
                logger.error(
                    f"Embedding batch {batch_index} failed after "
                    f"{EMBED_RETRY_COUNT} attempts: {exc}"
                )

    raise RuntimeError(
        f"Embedding batch {batch_index} exhausted {EMBED_RETRY_COUNT} retries"
    ) from last_error

"""
Embedding 客户端（基于 LiteLLM SDK）。

职责：
- 调用 litellm.aembedding 生成文本向量
- 与 LLM 聊天补全完全解耦，独立管理 embedding 调用

设计：
- 纯异步函数，无状态，不使用类封装
- API Key 由 litellm 从环境变量 OPENAI_API_KEY 自动读取
- 调用失败直接抛异常，不做静默处理
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import litellm

logger = logging.getLogger(__name__)

# 单批次最大文本数，避免单次请求过大
EMBED_BATCH_SIZE = 32


async def embed_texts(
    model: str,
    api_base: str,
    texts: Sequence[str],
) -> list[list[float]]:
    """
    使用 LiteLLM SDK 生成文本 embedding 向量。

    参数：
    - model: litellm 模型标识，例如 "litellm_proxy/Embedding-3-Small"
    - api_base: LiteLLM Proxy 地址，例如 "http://litellm-internal.example.com/"
    - texts: 待向量化的文本列表

    返回：
    - 与 texts 等长的 embedding 向量列表

    注意：
    - API Key 由 litellm 自动从环境变量 OPENAI_API_KEY 读取，无需显式传入
    - 超过 EMBED_BATCH_SIZE 的文本会自动分批处理
    """
    if not texts:
        raise ValueError("texts must not be empty")

    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = list(texts[i : i + EMBED_BATCH_SIZE])
        logger.info(f"Embedding request: model={model}, batch={len(batch)}, offset={i}")

        response = await litellm.aembedding(
            model=model,
            api_base=api_base,
            input=batch,
        )

        batch_embeddings = [list(item.embedding) for item in response.data]
        if len(batch_embeddings) != len(batch):
            raise RuntimeError(
                f"Embedding response length mismatch: expected {len(batch)}, got {len(batch_embeddings)}"
            )
        all_embeddings.extend(batch_embeddings)

    dim = len(all_embeddings[0]) if all_embeddings else 0
    logger.info(f"Embedding ok: total={len(all_embeddings)}, dim={dim}")
    return all_embeddings

"""
LLM Client（基于 OpenAI SDK，对接 LiteLLM Proxy）。

目标：
- **尽量薄**：只做协议适配与错误处理
- **统一接口**：通过 OpenAI-compatible API 访问 LiteLLM Proxy
- **严格 JSON**：planner/reviewer 等阶段需要可机读输出时，必须 schema 校验
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import httpx
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    """OpenAI chat message 的最小结构。"""

    role: str
    content: str


def _normalize_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


class OpenAICompatLLMClient:
    """
    通过 LiteLLM 调用 LLM（支持 100+ 模型）。

    LiteLLM 优势：
    - 统一接口调用不同 LLM 提供商
    - 自动处理 retry/fallback
    - 内置 cost tracking 和 logging
    """

    def __init__(self, api_key: str, base_url: str, http_client: httpx.AsyncClient, model: str) -> None:
        """
        - api_key: LLM API key
        - base_url: OpenAI-compatible base URL（LiteLLM Proxy 的地址）
        - http_client: 复用 httpx.AsyncClient 连接池
        - model: 模型名（例如 `claude-sonnet-4`，由 LiteLLM Proxy 路由）
        """
        self._base_url = _normalize_base_url(base_url=base_url)
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=self._base_url, http_client=http_client)

    async def complete_text(self, messages: Sequence[ChatMessage]) -> str:
        """
        调用 LiteLLM completion 并返回纯文本 content。

        注意：
        - LiteLLM 内部处理 retry 逻辑
        - 出错直接抛异常，便于上游统一处理/告警
        """
        try:
            logger.info(f"LLM request: model={self._model}, messages={len(messages)} msg(s)")
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[m.model_dump() for m in messages],
            )
        except OpenAIError as exc:
            logger.error(f"LLM API error: {exc}")
            raise
        except httpx.HTTPError as exc:
            logger.error(f"LLM HTTP error: {exc}")
            raise

        content = response.choices[0].message.content
        if content is None:
            logger.error("LLM returned None content")
            raise RuntimeError("LLM returned None content")

        logger.info(f"LLM response: {len(content)} chars")
        return str(content)

    async def complete_json(self, messages: Sequence[ChatMessage], schema: type[BaseModel]) -> BaseModel:
        """
        约定：让模型输出"纯 JSON"，然后做严格 schema 校验。

        - **为什么必须 JSON-only**：避免 markdown/自然语言导致无法自动回写 GitLab
        - **失败策略**：解析失败/校验失败直接抛错（宁可失败也不要写入错误评论）
        - **JSON mode**：使用 response_format 确保返回纯 JSON（不包含 markdown 代码块）
        """
        try:
            logger.info(f"LLM JSON request: model={self._model}, schema={schema.__name__}")
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[m.model_dump() for m in messages],
                response_format={"type": "json_object"},
            )
        except OpenAIError as exc:
            logger.error(f"LLM API error: {exc}")
            raise
        except httpx.HTTPError as exc:
            logger.error(f"LLM HTTP error: {exc}")
            raise

        content = response.choices[0].message.content
        if content is None:
            logger.error("LLM returned None content")
            raise RuntimeError("LLM returned None content")

        logger.info(f"LLM JSON response: {len(content)} chars")

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.error(f"Invalid JSON from LLM. Raw content: {content}")
            raise ValueError(f"LLM did not return valid JSON. Raw: {content}") from exc

        try:
            validated = schema.model_validate(parsed)
        except ValidationError as exc:
            logger.error(f"Schema validation failed: {exc}")
            raise ValueError(f"LLM JSON does not match schema {schema.__name__}: {exc}") from exc

        logger.info(f"Successfully validated JSON to {schema.__name__}")
        return validated

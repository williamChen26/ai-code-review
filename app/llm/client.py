"""
LLM Client（基于 LiteLLM SDK）。

目标：
- **尽量薄**：只做协议适配与错误处理
- **统一接口**：通过 LiteLLM SDK 调用各种模型
- **严格 JSON**：planner/reviewer 等阶段需要可机读输出时，必须 schema 校验

API Key 由 litellm 从环境变量 OPENAI_API_KEY 自动读取，无需显式传入。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import litellm
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

LLM_MODEL = "litellm_proxy/claude-sonnet-4"


class ChatMessage(BaseModel):
    """LLM chat message 的最小结构。"""

    role: str
    content: str


class LiteLLMClient:
    """
    通过 LiteLLM SDK 调用 LLM（支持 100+ 模型）。

    LiteLLM 优势：
    - 统一接口调用不同 LLM 提供商
    - 自动处理 retry/fallback
    - 内置 cost tracking 和 logging
    - API Key 自动从环境变量 OPENAI_API_KEY 读取
    """

    def __init__(self, base_url: str) -> None:
        """
        - base_url: LiteLLM Proxy 地址
        """
        self._base_url = base_url.rstrip("/")

    async def complete_text(self, messages: Sequence[ChatMessage]) -> str:
        """
        调用 LiteLLM completion 并返回纯文本 content。

        注意：
        - LiteLLM 内部处理 retry 逻辑
        - 出错直接抛异常，便于上游统一处理/告警
        """
        logger.info(f"LLM request: model={LLM_MODEL}, messages={len(messages)} msg(s)")
        response = await litellm.acompletion(
            model=LLM_MODEL,
            api_base=self._base_url,
            messages=[m.model_dump() for m in messages],
        )

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
        logger.info(f"LLM JSON request: model={LLM_MODEL}, schema={schema.__name__}")
        response = await litellm.acompletion(
            model=LLM_MODEL,
            api_base=self._base_url,
            messages=[m.model_dump() for m in messages],
            response_format={"type": "json_object"},
        )

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

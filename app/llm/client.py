"""
LLM Client（基于 LiteLLM）。

目标：
- **尽量薄**：只做协议适配与错误处理
- **统一接口**：使用 LiteLLM 支持 100+ LLM 模型
- **严格 JSON**：planner/reviewer 等阶段需要可机读输出时，必须 schema 校验
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import litellm
from litellm import acompletion
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    """OpenAI chat message 的最小结构。"""

    role: str
    content: str


class OpenAICompatLLMClient:
    """
    通过 LiteLLM 调用 LLM（支持 100+ 模型）。

    LiteLLM 优势：
    - 统一接口调用不同 LLM 提供商
    - 自动处理 retry/fallback
    - 内置 cost tracking 和 logging
    """

    def __init__(self, api_key: str, base_url: str, http_client: object, model: str) -> None:
        """
        - api_key: LLM API key
        - base_url: API base URL（LiteLLM 会自动处理不同提供商的 URL）
        - http_client: 保留参数以兼容现有代码，但 LiteLLM 内部管理连接
        - model: 模型名（例如 `claude-sonnet-4`，LiteLLM 会自动识别提供商）
        """
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        
        # 配置 LiteLLM
        litellm.api_key = api_key
        litellm.api_base = base_url
        
        # 启用详细日志以便调试
        litellm.set_verbose = False

    async def complete_text(self, messages: Sequence[ChatMessage]) -> str:
        """
        调用 LiteLLM completion 并返回纯文本 content。

        注意：
        - LiteLLM 内部处理 retry 逻辑
        - 出错直接抛异常，便于上游统一处理/告警
        """
        try:
            logger.info(f"LLM request: model={self._model}, messages={len(messages)} msg(s)")
            
            response = await acompletion(
                model=self._model,
                messages=[m.model_dump() for m in messages],
                api_key=self._api_key,
                api_base=self._base_url,
            )
            
            content = response.choices[0].message.content
            if content is None:
                raise RuntimeError("LLM returned None content")
            
            logger.info(f"LLM response: {len(content)} chars")
            return str(content)
            
        except Exception as exc:
            logger.error(f"LLM API error: {exc}")
            raise RuntimeError(f"LLM API error: {exc}") from exc

    async def complete_json(self, messages: Sequence[ChatMessage], schema: type[BaseModel]) -> BaseModel:
        """
        约定：让模型输出"纯 JSON"，然后做严格 schema 校验。

        - **为什么必须 JSON-only**：避免 markdown/自然语言导致无法自动回写 GitLab
        - **失败策略**：解析失败/校验失败直接抛错（宁可失败也不要写入错误评论）
        - **JSON mode**：使用 response_format 确保返回纯 JSON（不包含 markdown 代码块）
        """
        try:
            logger.info(f"LLM JSON request: model={self._model}, schema={schema.__name__}")
            
            # 使用 response_format 强制 JSON 输出
            response = await acompletion(
                model=self._model,
                messages=[m.model_dump() for m in messages],
                api_key=self._api_key,
                api_base=self._base_url,
                response_format={"type": "json_object"},
            )
            
            content = response.choices[0].message.content
            if content is None:
                raise RuntimeError("LLM returned None content")
            
            logger.info(f"LLM JSON response: {len(content)} chars")
            
            # 解析并验证 JSON
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                logger.error(f"Invalid JSON from LLM. Raw content: {content}")
                raise ValueError(f"LLM did not return valid JSON. Raw: {content}") from exc
            
            # Pydantic 校验
            try:
                validated = schema.model_validate(parsed)
                logger.info(f"Successfully validated JSON to {schema.__name__}")
                return validated
            except Exception as exc:
                logger.error(f"Schema validation failed: {exc}")
                raise ValueError(f"LLM JSON does not match schema {schema.__name__}: {exc}") from exc
                
        except Exception as exc:
            if isinstance(exc, (ValueError, RuntimeError)):
                raise
            logger.error(f"LLM API error: {exc}")
            raise RuntimeError(f"LLM API error: {exc}") from exc

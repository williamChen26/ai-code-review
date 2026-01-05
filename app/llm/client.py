"""
LLM Client（OpenAI-compatible）。

目标：
- **尽量薄**：只做协议适配与错误处理
- **可替换**：未来可换 LiteLLM/自建网关/不同模型
- **严格 JSON**：planner/reviewer 等阶段需要可机读输出时，必须 schema 校验
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import httpx
from pydantic import BaseModel


class ChatMessage(BaseModel):
    """OpenAI chat message 的最小结构。"""

    role: str
    content: str


class OpenAICompatLLMClient:
    """
    通过 OpenAI-compatible 接口调用 LLM。

    你后续只需要提供：
    - base_url（例如 https://xxx/v1 的上级，或 https://xxx 然后我们拼 /v1）
    - api_key
    - model（例如 claude-sonnet-4，按你的网关定义）
    """

    def __init__(self, api_key: str, base_url: str, http_client: httpx.AsyncClient, model: str) -> None:
        """
        - api_key: 你的网关或 OpenAI-compatible 服务的 key
        - base_url: `https://host` 或 `https://host/v1`
        - http_client: 复用的 httpx.AsyncClient
        - model: 模型名（例如 `claude-sonnet-4`，取决于你的网关）
        """
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client
        self._model = model

    def _headers(self) -> dict[str, str]:
        """OpenAI-compatible 通常使用 Bearer token。"""
        return {"Authorization": f"Bearer {self._api_key}"}

    def _chat_completions_url(self) -> str:
        """拼接 chat/completions URL，兼容 base_url 是否带 /v1。"""
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/chat/completions"
        return f"{self._base_url}/v1/chat/completions"

    async def complete_text(self, messages: Sequence[ChatMessage]) -> str:
        """
        调用 chat.completions 并返回纯文本 content。

        注意：
        - 这里不做 retry（后续可以放在 infra 层统一做）
        - 出错直接抛 RuntimeError，便于上游统一处理/告警
        """
        url = self._chat_completions_url()
        payload = {
            "model": self._model,
            "messages": [m.model_dump() for m in messages],
        }
        response = await self._http_client.post(url, headers=self._headers(), json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"LLM API error {response.status_code}: {response.text}")

        data = response.json()
        try:
            return str(data["choices"][0]["message"]["content"])
        except KeyError as exc:
            raise RuntimeError(f"Unexpected LLM response shape: {data}") from exc

    async def complete_json(self, messages: Sequence[ChatMessage], schema: type[BaseModel]) -> BaseModel:
        """
        约定：让模型输出“纯 JSON”，然后做严格 schema 校验。

        - **为什么必须 JSON-only**：避免 markdown/自然语言导致无法自动回写 GitLab
        - **失败策略**：解析失败/校验失败直接抛错（宁可失败也不要写入错误评论）
        """
        text = await self.complete_text(messages=messages)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM did not return valid JSON. Raw: {text}") from exc
        return schema.model_validate(parsed)

from __future__ import annotations

"""
最小 ReAct runtime（受控）。

设计目标：
- **你写流程**：外部决定 max_steps、提供 tool_executor、控制预算
- **模型只输出结构化指令**：JSON-only（action 或 final）
- **工具必须确定性**：可测试、可复现
"""

import json
from collections.abc import Callable

from app.agent.prompt import build_react_instructions
from app.agent.schemas import AgentAction
from app.agent.schemas import AgentFinal
from app.agent.schemas import AgentStep
from app.agent.schemas import ToolContext
from app.llm.client import ChatMessage
from app.llm.client import OpenAICompatLLMClient

ToolObservation = str | dict[str, int] | dict[str, list[str]] | dict[str, str]
ToolExecutor = Callable[[AgentAction, ToolContext], ToolObservation]


async def run_react_agent(
    llm_client: OpenAICompatLLMClient,
    user_prompt: str,
    tool_ctx: ToolContext,
    tool_executor: ToolExecutor,
    max_steps: int,
) -> str:
    """
    运行受控 ReAct loop。

    - 输入：用户提示、工具上下文、工具执行器、最大步数
    - 输出：最终的自然语言审查结论（由模型在 final.answer 给出）
    - 失败：模型输出非 JSON/不符合 schema 会直接抛错（不要继续执行）
    """
    if max_steps <= 0:
        raise ValueError("max_steps must be > 0")

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=build_react_instructions()),
        ChatMessage(role="user", content=user_prompt),
    ]

    for _ in range(max_steps):
        # 1) 让模型给出下一步：action 或 final（必须 JSON-only）
        raw = await llm_client.complete_text(messages=messages)
        step = _parse_agent_step(raw=raw)

        if isinstance(step, AgentFinal):
            return step.answer

        # 2) 执行工具（确定性）
        observation = tool_executor(step, tool_ctx)

        # 3) 把模型的 action 原文和 observation 回填给模型，进入下一轮
        messages.append(ChatMessage(role="assistant", content=raw))
        messages.append(
            ChatMessage(
                role="user",
                content=f'{{"observation": {json.dumps(observation, ensure_ascii=False)}}}',
            )
        )

    return "Review incomplete (step limit reached)"


def _parse_agent_step(raw: str) -> AgentStep:
    """将模型输出的 JSON 解析为 `AgentStep`（action/final）。"""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Agent output is not valid JSON. Raw: {raw}") from exc
    return AgentStep.model_validate(parsed)



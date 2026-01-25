"""
Risk Planner（LLM 单次输出，不 loop）。

目标：
- 让模型只做“规划”：哪些文件高风险、关注点是什么、review 深度如何
- 输出必须是严格 JSON，并通过 schema 校验

为什么 planner 不 loop：
- planner 的职责是“定方向”，不是“边查边想”
- loop 容易偏航、浪费预算；真正需要工具/推理的环节放到文件级 reviewer
"""
from __future__ import annotations

from app.llm.client import ChatMessage
from app.llm.client import OpenAICompatLLMClient
from app.review.models import ReviewContext
from app.review.models import RiskPlan
import logging

logger = logging.getLogger(__name__)


def _planner_system_prompt() -> str:
    """planner 的 system prompt：强制模型 JSON-only 输出。"""
    return (
        "你是资深代码审查工程师。"
        "你必须输出严格 JSON（不要 markdown、不要解释），用于决定本次 MR 的风险规划。"
    )


def _planner_user_prompt(context: ReviewContext) -> str:
    """planner 的 user prompt：只提供文件清单（不提供 diff，避免 planner 过度推理）。"""
    files = "\n".join([f"- {c.path} ({c.language})" for c in context.changes])
    return (
        "基于这次 PR/MR 的变更文件列表，生成风险规划 JSON：\n"
        '必须符合 schema: {"highRiskFiles":[...],"reviewFocus":[...],"reviewDepth":"shallow|normal|deep"}\n'
        "规则：\n"
        "- highRiskFiles 必须是本次变更文件的子集（用 path 字符串）\n"
        "- reviewFocus 给 2~5 个关键词（security, error-handling, performance, correctness, maintainability 等）\n"
        "- reviewDepth 选 deep/normal/shallow\n\n"
        f"变更文件：\n{files}\n"
    )


async def plan_risk(llm_client: OpenAICompatLLMClient, context: ReviewContext) -> RiskPlan:
    """
    执行 risk planning，并做工程侧兜底过滤：
    - highRiskFiles 必须属于本次变更文件（避免模型输出不存在路径）
    """
    messages = [
        ChatMessage(role="system", content=_planner_system_prompt()),
        ChatMessage(role="user", content=_planner_user_prompt(context=context)),
    ]
    result = await llm_client.complete_json(messages=messages, schema=RiskPlan)
    logger.info(f"Risk planning result: {result}")
    if not isinstance(result, RiskPlan):
        raise TypeError("LLM risk plan did not validate to RiskPlan")

    changed_paths = {c.path for c in context.changes}
    filtered = [p for p in result.highRiskFiles if p in changed_paths]
    return RiskPlan(highRiskFiles=filtered, reviewFocus=result.reviewFocus, reviewDepth=result.reviewDepth)



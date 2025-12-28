"""
文件级 Review（当前为“非 ReAct 的最小版本”）。

为什么这里先做成“逐文件 JSON 输出”：
- 先把闭环跑通（diff -> LLM -> comment）
- reviewer 的输入输出稳定后，再把内部实现替换为“受控 ReAct + tools”

注意：
- 这里只基于 diff（没有完整仓库上下文），所以建议描述要保守、可验证。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm.client import ChatMessage
from app.llm.client import OpenAICompatLLMClient
from app.review.models import FileChange
from app.review.models import ReviewComment
from app.review.models import RiskPlan


class FileReviewResult(BaseModel):
    """LLM 文件级输出 schema：一个文件可以给多条建议。"""

    comments: list[ReviewComment] = Field(default_factory=list)


def _truncate_text(text: str, max_chars: int) -> str:
    """控制 diff 输入长度，避免超出模型上下文/预算。"""
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...TRUNCATED..."


def _file_review_system_prompt() -> str:
    """reviewer 的 system prompt：强制 JSON-only 输出。"""
    return (
        "你是资深代码审查工程师。"
        "你必须输出严格 JSON（不要 markdown、不要解释），用于生成可直接贴到 GitLab 评论的建议。"
    )


def _file_review_user_prompt(file_change: FileChange, plan: RiskPlan) -> str:
    """reviewer 的 user prompt：给当前文件 diff + planner 的关注点。"""
    focus = ", ".join(plan.reviewFocus)
    diff = _truncate_text(text=file_change.diff, max_chars=12000)
    return (
        "请只基于 diff 做代码审查，输出 JSON：\n"
        '{"comments":[{"path":"...","message":"...","severity":"info|warning|error"}]}\n'
        "要求：\n"
        "- path 必须等于当前文件 path\n"
        "- message 要具体、可执行，必要时指出风险与修复建议\n"
        f"- 重点关注：{focus}\n\n"
        f"path: {file_change.path}\n"
        f"language: {file_change.language}\n"
        f"diff:\n{diff}\n"
    )


async def review_high_risk_files(
    llm_client: OpenAICompatLLMClient,
    changes: list[FileChange],
    plan: RiskPlan,
) -> list[ReviewComment]:
    """
    只 review planner 选出的 highRiskFiles。

    设计点：
    - 将 changes 按 path 建索引，保证选择是确定性的
    - 最后再做一次 path 白名单过滤，避免模型“胡写路径”
    """
    by_path: dict[str, FileChange] = {c.path: c for c in changes}
    selected: list[FileChange] = [by_path[p] for p in plan.highRiskFiles if p in by_path]

    comments: list[ReviewComment] = []
    for file_change in selected:
        # 逐文件调用，便于后续做并发/预算控制/失败重试
        messages = [
            ChatMessage(role="system", content=_file_review_system_prompt()),
            ChatMessage(role="user", content=_file_review_user_prompt(file_change=file_change, plan=plan)),
        ]
        result = await llm_client.complete_json(messages=messages, schema=FileReviewResult)
        if not isinstance(result, FileReviewResult):
            raise TypeError("LLM file review did not validate to FileReviewResult")
        comments.extend(result.comments)

    # 限制：确保每条 comment path 与变更文件一致
    allowed_paths = {c.path for c in changes}
    return [c for c in comments if c.path in allowed_paths]



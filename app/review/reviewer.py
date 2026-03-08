"""
文件级 Review（当前为"非 ReAct 的最小版本"）。

上下文来源（结构化 symbol context）：
- PR diff: 当前文件的变更
- Changed symbols: 被修改的 symbol 完整代码
- Related symbols: 语义相关的 symbol（来自向量搜索 + 调用关系）
- File summary: 文件级摘要（条件性补充，MVP 默认关闭）

Prompt 组织：
- system: 强制 JSON-only 输出
- user: diff + structured context + review focus
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm.client import ChatMessage
from app.llm.client import LiteLLMClient
from app.review.context_retrieval import ReviewContextPackage
from app.review.context_retrieval import format_context_package
from app.review.models import FileChange
from app.review.models import ReviewComment
from app.review.models import RiskPlan


class FileReviewResult(BaseModel):
    """LLM 文件级输出 schema：一个文件可以给多条建议。"""

    comments: list[ReviewComment] = Field(default_factory=list)


def _truncate_text(text: str, max_chars: int) -> str:
    """控制文本输入长度，避免超出模型上下文/预算。"""
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...TRUNCATED..."


def _file_review_system_prompt() -> str:
    """reviewer 的 system prompt：强制 JSON-only 输出。"""
    return (
        "你是资深代码审查工程师。"
        "你必须输出严格 JSON（不要 markdown、不要解释），用于生成可直接贴到代码评审评论区的建议。"
        "你会收到 diff、被修改的 symbol 代码、以及相关上下文 symbol 代码。"
        "请结合上下文做更精准的审查。"
    )


def _file_review_user_prompt(
    file_change: FileChange,
    plan: RiskPlan,
    context_package: ReviewContextPackage,
) -> str:
    """reviewer 的 user prompt：diff + 结构化 symbol context + review focus。"""
    focus = ", ".join(plan.reviewFocus)
    diff = _truncate_text(text=file_change.diff, max_chars=12000)
    context_text = format_context_package(package=context_package)
    context_section = context_text if context_text else "无可用上下文"

    return (
        "请基于 diff 和提供的上下文做代码审查，输出 JSON：\n"
        '{"comments":[{"path":"...","message":"...","severity":"info|warning|error"}]}\n'
        "要求：\n"
        "- path 必须等于当前文件 path\n"
        "- message 要具体、可执行，必要时指出风险与修复建议\n"
        "- 结合上下文中的 symbol 代码，判断修改是否破坏了调用方/被调用方的契约\n"
        f"- 重点关注：{focus}\n\n"
        f"## 上下文\n{context_section}\n\n"
        f"## 当前文件\n"
        f"path: {file_change.path}\n"
        f"language: {file_change.language}\n\n"
        f"## Diff\n```\n{diff}\n```\n"
    )


async def review_high_risk_files(
    llm_client: LiteLLMClient,
    changes: list[FileChange],
    plan: RiskPlan,
    context_by_path: dict[str, ReviewContextPackage],
) -> list[ReviewComment]:
    """只 review planner 选出的 highRiskFiles。

    设计点：
    - 将 changes 按 path 建索引，保证选择是确定性的
    - 接收结构化的 ReviewContextPackage（而非纯文本）
    - 最后再做一次 path 白名单过滤，避免模型"胡写路径"
    """
    by_path: dict[str, FileChange] = {c.path: c for c in changes}
    selected: list[FileChange] = [by_path[p] for p in plan.highRiskFiles if p in by_path]

    comments: list[ReviewComment] = []
    for file_change in selected:
        context_package = context_by_path.get(
            file_change.path, ReviewContextPackage(),
        )
        # 逐文件调用，便于后续做并发/预算控制/失败重试
        messages = [
            ChatMessage(role="system", content=_file_review_system_prompt()),
            ChatMessage(
                role="user",
                content=_file_review_user_prompt(
                    file_change=file_change,
                    plan=plan,
                    context_package=context_package,
                ),
            ),
        ]
        result = await llm_client.complete_json(messages=messages, schema=FileReviewResult)
        if not isinstance(result, FileReviewResult):
            raise TypeError("LLM file review did not validate to FileReviewResult")
        comments.extend(result.comments)

    # 限制：确保每条 comment path 与变更文件一致
    allowed_paths = {c.path for c in changes}
    return [c for c in comments if c.path in allowed_paths]



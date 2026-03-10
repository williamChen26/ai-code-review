"""
文件级 Review（当前为"非 ReAct 的最小版本"）。

上下文来源（结构化三层 FileReviewContext）：
- review_target: 审查目标文件信息
- context_package: diff + changed symbols + related symbols（+ 可扩展的 file/module 上下文）
- decision_trace: 上下文构建决策追踪

Prompt 组织（基于 LLM 注意力研究优化）：
- system: 角色定义 + 输出格式 + 审查准则（优先级加权）
- user: 审查目标 → 上下文（背景先行） → diff（利用 recency bias 放末尾）
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm.client import ChatMessage
from app.llm.client import LiteLLMClient
from app.review.context_retrieval import FileReviewContext
from app.review.context_retrieval import format_review_context
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
    """reviewer 的 system prompt：角色 + 输出格式 + 优先级加权审查准则。"""
    return (
        "你是资深代码审查工程师。"
        "审查安全问题时像攻击者一样思考，审查可靠性时像混沌工程师一样思考，"
        "审查正确性时像终端用户一样思考。\n\n"
        "## 输出格式\n"
        "只输出合法 JSON，不要 markdown 包裹，不要解释文本。\n"
        "Schema: "
        '{"comments":[{"path":"<必须等于目标文件路径>",'
        '"message":"<具体、可执行的反馈>",'
        '"severity":"info|warning|error"}]}\n\n'
        "## 严重性定义\n"
        "- error: Bug、安全漏洞、数据丢失风险、破坏 API 契约\n"
        "- warning: 潜在边界情况、错误处理缺失、意图不清、代码异味\n"
        "- info: 风格改进、可读性建议、小幅优化\n\n"
        "## 审查准则\n"
        "- 按优先级分配注意力：正确性和安全性 > 可靠性 > 性能 > 可维护性\n"
        "- 必须具体：引用确切的代码元素（函数名、变量名）\n"
        "- 尽量提供具体修复建议\n"
        "- 结合 Related Symbols 判断修改是否影响调用方/被调用方的契约\n"
        "- 如果代码没有真正的问题，返回空 comments 数组——这是有价值的信号，不要编造问题"
    )


def _file_review_user_prompt(
    review_context: FileReviewContext,
    plan: RiskPlan,
) -> str:
    """reviewer 的 user prompt：目标 → 上下文（背景先行） → diff（末尾，利用 recency bias）。"""
    target = review_context.review_target
    pkg = review_context.context_package

    focus = ", ".join(plan.reviewFocus)
    diff = _truncate_text(text=pkg.diff, max_chars=12000)
    context_text = format_review_context(context=review_context)
    context_section = context_text if context_text else "（无可用上下文 symbol）"

    change_type_parts: list[str] = []
    if target.is_new_file:
        change_type_parts.append("新文件")
    if target.is_deleted_file:
        change_type_parts.append("删除文件")
    if target.is_renamed_file:
        change_type_parts.append("重命名文件")
    change_type = ", ".join(change_type_parts) if change_type_parts else "修改文件"

    return (
        "# 审查任务\n\n"
        "## 目标文件\n"
        f"- 文件: {target.file}\n"
        f"- 语言: {target.language}\n"
        f"- 变更类型: {change_type}\n"
        f"- 审查焦点: {focus}\n"
        f"- 审查深度: {plan.reviewDepth}\n\n"
        f"## 上下文\n{context_section}\n\n"
        "## Diff\n"
        "请基于以上上下文，审查以下变更：\n"
        f"```\n{diff}\n```\n"
    )


async def review_high_risk_files(
    llm_client: LiteLLMClient,
    plan: RiskPlan,
    context_by_path: dict[str, FileReviewContext],
) -> list[ReviewComment]:
    """只 review planner 选出的 highRiskFiles。

    设计点：
    - FileReviewContext 已包含审查所需的全部信息（目标 + diff + 上下文）
    - 最后做一次 path 白名单过滤，避免模型"胡写路径"
    """
    selected_paths: list[str] = [p for p in plan.highRiskFiles if p in context_by_path]

    comments: list[ReviewComment] = []
    for path in selected_paths:
        review_context = context_by_path[path]
        messages = [
            ChatMessage(role="system", content=_file_review_system_prompt()),
            ChatMessage(
                role="user",
                content=_file_review_user_prompt(
                    review_context=review_context,
                    plan=plan,
                ),
            ),
        ]
        result = await llm_client.complete_json(messages=messages, schema=FileReviewResult)
        if not isinstance(result, FileReviewResult):
            raise TypeError("LLM file review did not validate to FileReviewResult")
        comments.extend(result.comments)

    allowed_paths: set[str] = set(context_by_path.keys())
    return [c for c in comments if c.path in allowed_paths]

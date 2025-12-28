from __future__ import annotations

"""
Synthesis（汇总输出）。

注意：
- 这里是**确定性输出**（不依赖 LLM），便于稳定回写 GitLab
- 未来如果你希望更“人类化”的表达，可以换成 LLM（但仍建议不 loop）
"""

from app.review.models import ReviewComment
from app.review.models import RiskPlan


def synthesize_gitlab_note_body(head_sha: str, plan: RiskPlan, comments: list[ReviewComment]) -> str:
    """
    将 planner + reviewer 的结果拼成一段 GitLab MR note 文本。

    - head_sha：用于标注 review 对应的 commit（便于版本化/幂等）
    - plan：risk planning 输出
    - comments：文件级 review 建议列表
    """
    focus = ", ".join(plan.reviewFocus)
    lines: list[str] = []
    lines.append(f"AI Code Review (commit: `{head_sha}`)")
    lines.append("")
    lines.append(f"- Review focus: **{focus}**")
    lines.append(f"- Review depth: **{plan.reviewDepth}**")
    lines.append("")

    if not comments:
        lines.append("未发现需要阻塞的明显问题（基于 diff 的有限上下文）。")
        return "\n".join(lines)

    lines.append("### 建议")
    for c in comments:
        lines.append(f"- **[{c.severity}]** `{c.path}`: {c.message}")

    return "\n".join(lines)



"""
Review 领域模型（Pydantic）。

用途：
- 明确各阶段输入/输出的数据结构
- 作为 LLM JSON 输出的 schema 校验（planner/reviewer 等）
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class FileChange(BaseModel):
    """单个文件的变更（从 GitLab changes/diff 归一化而来）。"""

    path: str
    diff: str
    language: str
    is_new_file: bool
    is_deleted_file: bool
    is_renamed_file: bool


class GitLabReviewSource(BaseModel):
    """GitLab 侧 review 源信息（写回/幂等使用）。"""

    kind: Literal["gitlab"]
    project_id: int
    mr_iid: int


class GitHubReviewSource(BaseModel):
    """GitHub 侧 review 源信息（写回/幂等使用）。"""

    kind: Literal["github"]
    owner: str
    repo: str
    pull_number: int


ReviewSource = Annotated[Union[GitLabReviewSource, GitHubReviewSource], Field(discriminator="kind")]


class ReviewContext(BaseModel):
    """一次代码评审的上下文（平台无关，供 planner/reviewer 使用）。"""

    source: ReviewSource
    head_sha: str
    repo_id: str
    changes: list[FileChange] = Field(default_factory=list)


class RiskPlan(BaseModel):
    """Risk planner 的结构化输出（必须 JSON-only）。"""

    highRiskFiles: list[str]
    reviewFocus: list[str]
    reviewDepth: Literal["shallow", "normal", "deep"]


class ReviewComment(BaseModel):
    """reviewer 输出的单条建议（目前用于全局 note 汇总）。"""

    path: str
    message: str
    severity: Literal["info", "warning", "error"]


# ---------------------------------------------------------------------------
# FileReviewContext 三层结构
# ---------------------------------------------------------------------------


class SymbolContext(BaseModel):
    """审查上下文中的 symbol 信息（SymbolRecord 的 LLM-facing 子集）。"""

    name: str
    kind: str
    file: str
    start_line: int
    end_line: int
    code: str


class ReviewTarget(BaseModel):
    """审查目标：标识正在审查的文件及其变更属性。"""

    file: str
    language: str
    is_new_file: bool
    is_deleted_file: bool
    is_renamed_file: bool


class ContextPackage(BaseModel):
    """分层上下文包：为 LLM 提供结构化的审查上下文。

    当前阶段提供 diff + symbol 级上下文；
    file/module 级上下文字段保留为 None，供后续扩展。
    """

    diff: str
    changed_symbols: list[SymbolContext]
    related_symbols: list[SymbolContext]
    file_summary: str | None = None
    file_excerpt: str | None = None
    module_summary: str | None = None


class ContextDecisionTrace(BaseModel):
    """上下文构建的决策追踪，用于调试和可观测性。"""

    has_changed_symbols: bool
    has_related_symbols: bool
    added_file_summary: bool
    added_file_excerpt: bool
    reasons: list[str]


class FileReviewContext(BaseModel):
    """单个文件的完整审查上下文。

    三层结构：
    - review_target: 审查目标（什么文件、什么类型的变更）
    - context_package: 上下文包（diff + symbols + 可扩展的 file/module 上下文）
    - decision_trace: 决策追踪（为什么选择了这些上下文）
    """

    review_target: ReviewTarget
    context_package: ContextPackage
    decision_trace: ContextDecisionTrace



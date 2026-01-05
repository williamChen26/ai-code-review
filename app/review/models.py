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
    changes: list[FileChange] = Field(default_factory=list)


class MergeRequestContext(BaseModel):
    """兼容：历史命名（仅 GitLab），后续可逐步迁移到 `ReviewContext`。"""

    project_id: int
    mr_iid: int
    head_sha: str
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



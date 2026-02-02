"""
GitLab Webhook / API response schemas（Pydantic）。

为什么要单独放 schema：
- GitLab 的 payload 结构复杂，直接用 dict 容易写错 key
- schema 校验失败会立刻暴露问题（比“默默 None”安全）

说明：
- 这里的字段只覆盖当前最小闭环所需子集，后续可按需补充
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GitLabUser(BaseModel):
    """Webhook 里的 user 子结构（只取 username）。"""

    username: str


class GitLabProject(BaseModel):
    """Webhook 里的 project 子结构（id/web_url）。"""

    id: int
    web_url: str


class GitLabMergeRequestAttributes(BaseModel):
    """（预留）MR attributes 的子集，目前未在主链路使用。"""

    iid: int
    target_branch: str
    source_branch: str
    last_commit: dict[str, object] = Field(min_length=1)


class GitLabMergeRequestObjectAttributes(BaseModel):
    """Merge request webhook 的 object_attributes 子结构。"""

    iid: int
    action: Literal["open", "update", "reopen", "merge", "close", "approved", "unapproved"]
    last_commit: dict[str, object]
    target_branch: str
    source_branch: str


class GitLabMergeRequestWebhookEvent(BaseModel):
    """Merge request webhook 的最小结构。"""

    object_kind: Literal["merge_request"]
    user: GitLabUser
    project: GitLabProject
    object_attributes: GitLabMergeRequestObjectAttributes


class GitLabDiffRef(BaseModel):
    """GitLab 返回的 diff refs（用于行内评论 position，后续会用到）。"""

    base_sha: str
    head_sha: str
    start_sha: str


class GitLabMRChange(BaseModel):
    """单个文件变更（包含 diff 字符串）。"""

    old_path: str
    new_path: str
    a_mode: str | None
    b_mode: str | None
    new_file: bool
    renamed_file: bool
    deleted_file: bool
    diff: str


class GitLabMergeRequestChanges(BaseModel):
    """MR changes API 返回结构（changes + diff_refs）。"""

    changes: list[GitLabMRChange]
    diff_refs: GitLabDiffRef


class GitLabNote(BaseModel):
    """MR note 返回结构。"""

    id: int
    body: str



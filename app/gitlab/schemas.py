"""
GitLab Webhook / API response schemas（Pydantic）。

为什么要单独放 schema：
- GitLab 的 payload 结构复杂，直接用 dict 容易写错 key
- schema 校验失败会立刻暴露问题（比"默默 None"安全）

说明：
- 这里的字段只覆盖当前最小闭环所需子集，后续可按需补充
- 基于 GitLab v4 API：https://docs.gitlab.com/ee/api/merge_requests.html
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, model_validator


# ---------------------------------------------------------------------------
# Webhook payload 子结构
# ---------------------------------------------------------------------------


class GitLabUser(BaseModel):
    """Webhook 里的 user 子结构（只取 username）。"""

    username: str


class GitLabProject(BaseModel):
    """Webhook 里的 project 子结构。

    - id: 项目数字 ID（用于 API 调用）
    - web_url: 项目页面地址
    - git_http_url: HTTPS clone 地址（自托管实例也返回此字段）
    """

    id: int
    web_url: str
    git_http_url: str


class GitLabLastCommit(BaseModel):
    """Webhook 中 last_commit 的强类型子集（只取 id 即 commit SHA）。"""

    id: str


def _state_to_action(state: str) -> str:
    """GitLab 有时只发 state 不发 action，按文档映射为 action。"""
    m = {"opened": "open", "closed": "close", "merged": "merge", "reopened": "reopen"}
    return m.get(state, "update")


class GitLabMergeRequestObjectAttributes(BaseModel):
    """Merge request webhook 的 object_attributes 子结构。

    GitLab 实际推送可能带 action 或仅带 state，这里兼容两种：
    - 有 action 时直接使用
    - 仅有 state 时从 state 推导 action（opened->open, closed->close, merged->merge, reopened->reopen）
    """

    iid: int
    action: Literal["open", "update", "reopen", "merge", "close", "approved", "unapproved"]
    last_commit: GitLabLastCommit
    target_branch: str
    source_branch: str
    state: str | None = None  # 原始 state，仅作兼容接收

    @model_validator(mode="before")
    @classmethod
    def action_from_state(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("action") is None and data.get("state") is not None:
            data = {**data, "action": _state_to_action(data["state"])}
        return data


class GitLabMergeRequestWebhookEvent(BaseModel):
    """Merge request webhook 的最小结构。"""

    object_kind: Literal["merge_request"]
    user: GitLabUser
    project: GitLabProject
    object_attributes: GitLabMergeRequestObjectAttributes


# ---------------------------------------------------------------------------
# MR Changes API 响应结构
# GET /projects/:id/merge_requests/:iid/changes
# ---------------------------------------------------------------------------


class GitLabDiffRef(BaseModel):
    """GitLab diff_refs（base/head/start SHA，用于行内评论 position）。"""

    base_sha: str
    head_sha: str
    start_sha: str


class GitLabMRChange(BaseModel):
    """单个文件变更。

    路径说明：
    - 普通修改：old_path == new_path
    - 新文件：new_file=True，old_path == new_path
    - 删除文件：deleted_file=True，old_path == new_path（path 保留原路径）
    - 重命名：renamed_file=True，old_path != new_path
    """

    old_path: str
    new_path: str
    a_mode: str | None
    b_mode: str | None
    new_file: bool
    renamed_file: bool
    deleted_file: bool
    diff: str


class GitLabMergeRequestChanges(BaseModel):
    """MR changes API 返回结构。

    overflow 说明（GitLab 10.8+）：
    - False：所有变更文件的 diff 均已完整返回
    - True：变更文件过多或 diff 过大，部分 diff 被截断
    - 可通过 access_raw_diffs=true 参数绕过数据库侧的大小限制
    """

    changes: list[GitLabMRChange]
    diff_refs: GitLabDiffRef | None = None
    overflow: bool = False


# ---------------------------------------------------------------------------
# MR Note API 响应结构
# POST /projects/:id/merge_requests/:iid/notes
# ---------------------------------------------------------------------------


class GitLabNote(BaseModel):
    """MR note（评论）返回结构。"""

    id: int
    body: str

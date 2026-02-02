"""
GitHub Webhook / API response schemas（Pydantic）。

说明：
- 字段只覆盖当前最小闭环需要的子集（PR webhook + list files）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class GitHubOwner(BaseModel):
    login: str


class GitHubRepository(BaseModel):
    name: str
    owner: GitHubOwner
    full_name: str
    clone_url: str


class GitHubPullRequestHead(BaseModel):
    sha: str
    ref: str


class GitHubPullRequestBase(BaseModel):
    ref: str


class GitHubPullRequest(BaseModel):
    number: int
    head: GitHubPullRequestHead
    base: GitHubPullRequestBase
    merged: bool


class GitHubPullRequestWebhookEvent(BaseModel):
    """
    GitHub `pull_request` webhook event（最小结构）。

    action: opened/reopened/synchronize 等
    """

    action: Literal[
        "opened",
        "reopened",
        "synchronize",
        "closed",
        "edited",
        "ready_for_review",
        "labeled",
        "unlabeled",
    ]
    pull_request: GitHubPullRequest
    repository: GitHubRepository


class GitHubPullRequestFile(BaseModel):
    """
    PR 文件列表 item（GET /pulls/{pull_number}/files）。

    patch 可能缺失（例如大文件/二进制/被截断），这种情况在 adapter 里会直接抛错。
    """

    filename: str
    status: Literal["added", "modified", "removed", "renamed", "changed", "copied"]
    patch: str | None



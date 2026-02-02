"""
GitHub -> Review domain adapter。

职责：
- 将 GitHub PR files/patch 转为平台无关的 `ReviewContext`
"""

from __future__ import annotations

from app.github.schemas import GitHubPullRequestFile
from app.indexing.indexer import build_repo_id
from app.review.context import infer_language_from_path
from app.review.models import FileChange
from app.review.models import GitHubReviewSource
from app.review.models import ReviewContext


def build_review_context_from_github_pull_request_files(
    owner: str,
    repo: str,
    pull_number: int,
    head_sha: str,
    files: list[GitHubPullRequestFile],
) -> ReviewContext:
    file_changes: list[FileChange] = []
    for f in files:
        if not isinstance(f.patch, str) or not f.patch:
            raise ValueError(f"GitHub file patch is missing or empty for: {f.filename}")
        file_changes.append(
            FileChange(
                path=f.filename,
                diff=f.patch,
                language=infer_language_from_path(path=f.filename),
                is_new_file=f.status == "added",
                is_deleted_file=f.status == "removed",
                is_renamed_file=f.status == "renamed",
            )
        )
    return ReviewContext(
        source=GitHubReviewSource(kind="github", owner=owner, repo=repo, pull_number=pull_number),
        head_sha=head_sha,
        repo_id=build_repo_id(provider="github", repo_key=f"{owner}/{repo}"),
        changes=file_changes,
    )



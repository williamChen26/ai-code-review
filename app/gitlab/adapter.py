"""
GitLab -> Review domain adapter。

职责：
- 将 GitLab API 的 changes/diff schema 转换为平台无关的 `ReviewContext`
- 只做数据归一化，不做业务决策
"""

from __future__ import annotations

from app.gitlab.schemas import GitLabMergeRequestChanges
from app.indexing.indexer import build_repo_id
from app.review.context import infer_language_from_path
from app.review.models import FileChange
from app.review.models import GitLabReviewSource
from app.review.models import ReviewContext


def build_review_context_from_gitlab_changes(
    project_id: int,
    mr_iid: int,
    head_sha: str,
    changes: GitLabMergeRequestChanges,
) -> ReviewContext:
    file_changes: list[FileChange] = []
    for c in changes.changes:
        path = c.new_path
        file_changes.append(
            FileChange(
                path=path,
                diff=c.diff,
                language=infer_language_from_path(path=path),
                is_new_file=c.new_file,
                is_deleted_file=c.deleted_file,
                is_renamed_file=c.renamed_file,
            )
        )
    return ReviewContext(
        source=GitLabReviewSource(kind="gitlab", project_id=project_id, mr_iid=mr_iid),
        head_sha=head_sha,
        repo_id=build_repo_id(provider="gitlab", repo_key=str(project_id)),
        changes=file_changes,
    )



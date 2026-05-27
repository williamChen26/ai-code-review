"""
GitLab -> Review domain adapter。

职责：
- 将 GitLab API 的 changes/diff schema 转换为平台无关的 `ReviewContext`
- 只做数据归一化，不做业务决策
"""

from __future__ import annotations

import logging

from app.gitlab.schemas import GitLabMergeRequestChanges
from app.indexing.indexer import build_repo_id
from app.review.context import infer_language_from_path
from app.review.models import FileChange
from app.review.models import GitLabReviewSource
from app.review.models import ReviewContext

logger = logging.getLogger(__name__)


def build_review_context_from_gitlab_changes(
    project_id: int,
    mr_iid: int,
    head_sha: str,
    changes: GitLabMergeRequestChanges,
) -> ReviewContext:
    """将 GitLab MR changes 转换为平台无关的 ReviewContext。

    路径选择策略：
    - 删除文件：使用 old_path（文件已不存在于新版本）
    - 重命名文件：使用 new_path（关注重命名后的路径）
    - 其他：使用 new_path
    """
    file_changes: list[FileChange] = []
    for c in changes.changes:
        # 跳过 diff 为空的文件（二进制文件或超大文件被截断）
        if not c.diff:
            logger.info(f"跳过无 diff 的文件: old_path={c.old_path}, new_path={c.new_path}")
            continue

        path = c.old_path if c.deleted_file else c.new_path

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

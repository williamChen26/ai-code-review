"""
Context Builder（非 AI）。

职责：
- 把 GitLab 的 changes/diff 转换为我们内部的 `MergeRequestContext`
- 做最少量的工程推断（例如通过扩展名推断语言）
"""

from __future__ import annotations

from app.gitlab.schemas import GitLabMergeRequestChanges
from app.review.models import FileChange
from app.review.models import MergeRequestContext


def infer_language_from_path(path: str) -> str:
    """
    通过文件扩展名推断语言。

    这是一个非常“工程”的步骤：不需要 LLM，且必须确定性。
    后续可以扩展成更完整的映射（或读取仓库配置）。
    """
    lowered = path.lower()
    if lowered.endswith(".py"):
        return "python"
    if lowered.endswith(".ts") or lowered.endswith(".tsx"):
        return "typescript"
    if lowered.endswith(".js") or lowered.endswith(".jsx"):
        return "javascript"
    if lowered.endswith(".go"):
        return "go"
    if lowered.endswith(".java"):
        return "java"
    if lowered.endswith(".rb"):
        return "ruby"
    if lowered.endswith(".php"):
        return "php"
    if lowered.endswith(".rs"):
        return "rust"
    if lowered.endswith(".sql"):
        return "sql"
    return "unknown"


def build_merge_request_context(
    project_id: int,
    mr_iid: int,
    head_sha: str,
    changes: GitLabMergeRequestChanges,
) -> MergeRequestContext:
    """
    将 GitLab MR changes 转为内部上下文对象。

    - project_id/mr_iid/head_sha：用于后续幂等与写回 GitLab
    - changes：包含每个文件的 diff/重命名/新增/删除信息
    """
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

    return MergeRequestContext(project_id=project_id, mr_iid=mr_iid, head_sha=head_sha, changes=file_changes)



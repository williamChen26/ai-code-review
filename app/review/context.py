"""
Context Builder（非 AI）。

职责：
- 把 GitLab 的 changes/diff 转换为我们内部的 `MergeRequestContext`
- 做最少量的工程推断（例如通过扩展名推断语言）
"""

from __future__ import annotations


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




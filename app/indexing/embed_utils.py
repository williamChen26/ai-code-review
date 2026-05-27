"""
Embedding 文本构建工具。

职责：
- 统一构建 symbol embedding 的输入文本
- 对超长 code 做截断，防止超出 embedding 模型 token 上限

设计：
- 索引侧（indexer）和查询侧（context_retrieval）共用同一函数
- 保证两侧生成的文本格式完全一致，向量空间对齐
"""

from __future__ import annotations

EMBED_MAX_CHARS = 24000
_TRUNCATION_MARKER = "\n... [truncated]"


def build_embedding_text(path: str, code: str) -> str:
    """构建 symbol embedding 的输入文本，超长 code 自动截断。

    格式（与 context_retrieval 查询端一致）：
        File: {path}

        Code:
        {code}

    截断策略：保留代码头部（函数签名 + 核心逻辑），丢弃尾部。
    头部包含的信息（函数名、参数类型、前几十行逻辑）语义价值最高。
    """
    truncated_code = truncate_code(code)
    return f"File: {path}\n\nCode:\n{truncated_code}"


def truncate_code(code: str) -> str:
    """按字符数截断 code，超限时保留头部并追加截断标记。"""
    if len(code) <= EMBED_MAX_CHARS:
        return code
    return code[:EMBED_MAX_CHARS] + _TRUNCATION_MARKER

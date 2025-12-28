from __future__ import annotations

"""
安全/风险模式扫描工具（极简版）。

特点：
- 确定性：只做字符串包含匹配
- 可测试：输入 diff，输出命中的 patterns

后续可扩展：
- 正则/AST 规则
- 语言特定规则集（SQL 注入、命令注入、XSS、危险反序列化等）
"""

from app.agent.schemas import FindRiskyPatternArgs
from app.agent.schemas import ToolContext


def find_risky_pattern(args: FindRiskyPatternArgs, ctx: ToolContext) -> dict[str, list[str]]:
    """
    在指定文件 diff 中查找 patterns（字符串包含）。

    - 输入：path + patterns
    - 输出：{"hits": [...]}（命中的 pattern 列表）
    """
    if args.path not in ctx.diff_by_path:
        raise KeyError(f"diff not found for path: {args.path}")
    diff = ctx.diff_by_path[args.path]
    hits: list[str] = []
    for pattern in args.patterns:
        if not pattern:
            raise ValueError("pattern must be non-empty")
        if pattern in diff:
            hits.append(pattern)
    return {"hits": hits}



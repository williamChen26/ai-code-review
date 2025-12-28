from __future__ import annotations

"""
复杂度分析工具（基于 diff 的近似值）。

说明：
- 真正的圈复杂度需要完整函数/文件，这里我们只有 diff，所以这是“提示风险用”的近似指标。
- 我们只取新增的 Python 行（diff 中 `+` 开头），用 AST 解析并统计分支节点。
"""

import ast

from app.agent.schemas import CalcPythonComplexityArgs
from app.agent.schemas import ToolContext


def calc_python_complexity(args: CalcPythonComplexityArgs, ctx: ToolContext) -> dict[str, int]:
    """
    一个可测试、确定性的近似复杂度：统计常见分支节点数量 + 1。
    注意：这里只能基于 diff（不拉取全文件），因此只用于“提示风险”，不是精确指标。
    """
    if args.path not in ctx.diff_by_path:
        raise KeyError(f"diff not found for path: {args.path}")

    # 只取新增行，减少解析噪音；解析失败时直接抛 SyntaxError（上游决定如何处理）
    code = _extract_added_python_lines_from_diff(diff=ctx.diff_by_path[args.path])
    if not code.strip():
        return {"complexity": 0}

    # 使用 AST 解析：比正则靠谱，且可测试
    tree = ast.parse(code)
    branch_nodes = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.With,
        ast.AsyncWith,
        ast.Match,
        ast.BoolOp,
    )
    count = 1
    for node in ast.walk(tree):
        if isinstance(node, branch_nodes):
            count += 1
    return {"complexity": count}


def _extract_added_python_lines_from_diff(diff: str) -> str:
    """从 unified diff 里提取新增代码行（去掉 diff 元信息）。"""
    lines: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("@@"):
            continue
        if line.startswith("+") and not line.startswith("++"):
            lines.append(line[1:])
    return "\n".join(lines)



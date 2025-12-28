from __future__ import annotations

"""
diff 工具：从某个文件的 diff 中截取前 N 行。

用途：
- 控制上下文长度（避免把整段 diff 都塞给模型）
- 让 ReAct/Reviewer 能“按需取片段”
"""

from app.agent.schemas import GetDiffChunkArgs
from app.agent.schemas import ToolContext


def get_diff_chunk(args: GetDiffChunkArgs, ctx: ToolContext) -> str:
    """
    返回指定文件 diff 的前 max_lines 行。

    - 输入：path + max_lines
    - 输出：字符串 chunk
    - 失败：path 不存在抛 KeyError；max_lines 非法抛 ValueError
    """
    if args.max_lines <= 0:
        raise ValueError("max_lines must be > 0")
    if args.path not in ctx.diff_by_path:
        raise KeyError(f"diff not found for path: {args.path}")
    diff = ctx.diff_by_path[args.path]
    lines = diff.splitlines()
    chunk = "\n".join(lines[: args.max_lines])
    return chunk



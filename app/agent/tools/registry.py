from __future__ import annotations

"""
工具注册/路由。

为什么需要 registry：
- 把“模型输出的 tool name”映射到具体的确定性函数
- 统一做未知工具名的错误处理
"""

from app.agent.schemas import AgentAction
from app.agent.schemas import ToolContext
from app.agent.tools.complexity import calc_python_complexity
from app.agent.tools.diff import get_diff_chunk
from app.agent.tools.security import find_risky_pattern


def execute_tool(action: AgentAction, ctx: ToolContext) -> object:
    """执行一个工具调用，并返回 observation（必须可 JSON 序列化）。"""
    call = action.call
    if call.name == "get_diff_chunk":
        return get_diff_chunk(args=call.args, ctx=ctx)
    if call.name == "find_risky_pattern":
        return find_risky_pattern(args=call.args, ctx=ctx)
    if call.name == "calc_python_complexity":
        return calc_python_complexity(args=call.args, ctx=ctx)
    raise ValueError(f"Unknown tool: {call.name}")



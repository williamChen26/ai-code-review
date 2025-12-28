from __future__ import annotations

"""
限流/预算控制（最小版本）。

为什么需要这个模块：
- Agent/ReAct 很容易“跑飞”消耗大量 token
- 生产环境必须有明确的预算与拒绝策略（宁可失败，不要失控）
"""


class RateLimitExceededError(RuntimeError):
    """超过预算/限流时抛出的错误类型。"""

    pass


def check_rate_limit(identity: str, budget: int, used: int) -> None:
    """
    最小限流检查。

    - identity: 例如 project_id/mr_iid/user 等组合（用于区分限流主体）
    - budget: 预算上限
    - used: 已使用量
    """
    if not identity:
        raise ValueError("identity must be non-empty")
    if budget <= 0:
        raise ValueError("budget must be > 0")
    if used < 0:
        raise ValueError("used must be >= 0")
    if used >= budget:
        raise RateLimitExceededError(f"Rate limit exceeded for {identity}: {used}/{budget}")



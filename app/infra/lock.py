"""
进程内并发控制。

提供：
- RepoLockManager: per-repo asyncio.Lock，保证同一 repo 的操作串行执行
- InFlightTracker: webhook 去重，防止同一 MR+SHA+action 被重复处理

设计说明：
- 当前单进程部署，asyncio.Lock 覆盖所有场景
- 如需多实例，可将 RepoLockManager 替换为 Postgres Advisory Lock（接口不变）
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class RepoLockManager:
    """per-repo asyncio.Lock 管理器，保证同一 repo 的操作串行执行。

    不同 repo 之间互不阻塞；同一 repo 的所有操作（sync/index/review）串行。
    锁粒度选择 repo 级而非 MR 级，因为 git 目录和索引数据都是 repo 维度共享的。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, repo_id: str) -> AsyncGenerator[None, None]:
        async with self._meta_lock:
            if repo_id not in self._locks:
                self._locks[repo_id] = asyncio.Lock()
            lock = self._locks[repo_id]
        logger.debug(f"等待 repo 锁: {repo_id}")
        async with lock:
            logger.debug(f"已获取 repo 锁: {repo_id}")
            yield
        logger.debug(f"已释放 repo 锁: {repo_id}")


class InFlightTracker:
    """追踪正在处理的 webhook 任务，用于去重。

    去重 key 格式: {repo_id}:{mr_iid}:{head_sha}:{action}
    - 相同 MR 的不同 SHA（新 push）不会被去重，因为内容变了需要重新 review
    - 相同 MR + 相同 SHA + 相同 action 的重复 webhook 会被跳过
    """

    def __init__(self) -> None:
        self._inflight: set[str] = set()
        self._lock = asyncio.Lock()

    async def try_start(self, key: str) -> bool:
        """尝试标记任务开始。返回 True 表示成功，False 表示已有相同任务在执行。"""
        async with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    async def finish(self, key: str) -> None:
        """标记任务完成，移除去重标记。"""
        async with self._lock:
            self._inflight.discard(key)

from __future__ import annotations

"""
缓存抽象（最小版本）。

当前提供：
- `Cache` Protocol：定义 get/set 接口
- `InMemoryCache`：便于本地运行/单元测试

后续扩展点：
- Redis 实现（生产可用）
- 幂等 key（MR IID + head SHA）存储
"""

from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Protocol


class Cache(Protocol):
    """缓存接口协议（用于依赖倒置，方便替换 Redis/Memory）。"""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


@dataclass
class InMemoryCache:
    """内存缓存：只用于开发/测试，不提供过期机制。"""

    store: MutableMapping[str, str]

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value



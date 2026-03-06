"""
调试工具模块。

提供结构化日志和步骤追踪功能，用于清晰追踪请求处理流程。

使用方式：
    from app.debug_utils import get_logger, step_tracker

    logger = get_logger(__name__)
    
    async def handle_request():
        with step_tracker("handle_webhook") as tracker:
            tracker.step("解析 webhook payload")
            # ... 业务逻辑
            tracker.step("获取 MR 变更")
            # ...
"""

from __future__ import annotations

import contextvars
import logging
import sys
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from typing import Any

# 请求级别的上下文变量
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
_current_step: contextvars.ContextVar[int] = contextvars.ContextVar("current_step", default=0)

# 是否启用调试模式（通过环境变量控制）
DEBUG_MODE = True


class RequestContextFilter(logging.Filter):
    """日志过滤器：注入 request_id 和 step 到日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get() or "-"
        record.step = _current_step.get()
        return True


def setup_logging(level: int = logging.DEBUG) -> None:
    """配置全局日志格式。"""
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-5s | [%(request_id)s] Step %(step)02d | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RequestContextFilter())
    
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """获取带上下文的 logger。"""
    logger = logging.getLogger(name)
    return logger


def set_request_id(request_id: str) -> None:
    """设置当前请求的 ID。"""
    _request_id.set(request_id)


def get_request_id() -> str:
    """获取当前请求的 ID。"""
    return _request_id.get()


def generate_request_id() -> str:
    """生成唯一请求 ID。"""
    return uuid.uuid4().hex[:8]


@dataclass
class StepTracker:
    """步骤追踪器：记录每一步的开始/结束和耗时。"""
    
    name: str
    logger: logging.Logger = field(default_factory=lambda: get_logger("tracker"))
    start_time: float = field(default_factory=time.time)
    step_times: list[tuple[int, str, float]] = field(default_factory=list)
    
    def step(self, description: str) -> None:
        """记录一个步骤。"""
        current = _current_step.get() + 1
        _current_step.set(current)
        
        now = time.time()
        elapsed = now - self.start_time
        
        if self.step_times:
            last_step, _, last_time = self.step_times[-1]
            step_duration = now - last_time
            self.logger.info(f"[+{step_duration:.2f}s] → {description}")
        else:
            self.logger.info(f"[开始] → {description}")
        
        self.step_times.append((current, description, now))
    
    def substep(self, description: str) -> None:
        """记录子步骤（不增加主步骤计数）。"""
        current = _current_step.get()
        now = time.time()
        elapsed = now - self.start_time
        self.logger.debug(f"    ↳ {description}")
    
    def finish(self) -> None:
        """完成追踪，输出摘要。"""
        total_time = time.time() - self.start_time
        self.logger.info(f"[完成] 总耗时: {total_time:.2f}s, 共 {len(self.step_times)} 步")
        
        if DEBUG_MODE and self.step_times:
            self.logger.debug("--- 步骤耗时摘要 ---")
            for i, (step_num, desc, step_time) in enumerate(self.step_times):
                if i + 1 < len(self.step_times):
                    duration = self.step_times[i + 1][2] - step_time
                else:
                    duration = time.time() - step_time
                self.logger.debug(f"  Step {step_num:02d}: {duration:6.2f}s | {desc}")


@contextmanager
def step_tracker(name: str) -> Generator[StepTracker, None, None]:
    """步骤追踪上下文管理器。"""
    request_id = get_request_id() or generate_request_id()
    set_request_id(request_id)
    _current_step.set(0)
    
    logger = get_logger(f"tracker.{name}")
    logger.info(f"{'=' * 50}")
    logger.info(f"开始流程: {name}")
    logger.info(f"{'=' * 50}")
    
    tracker = StepTracker(name=name, logger=logger)
    try:
        yield tracker
    except Exception as e:
        logger.error(f"[错误] 流程 {name} 在 Step {_current_step.get()} 失败: {e}")
        raise
    finally:
        tracker.finish()


def log_function_call(func_name: str, **kwargs: Any) -> None:
    """记录函数调用的参数（用于调试）。"""
    if not DEBUG_MODE:
        return
    logger = get_logger("debug.calls")
    params = ", ".join(f"{k}={_truncate_value(v)}" for k, v in kwargs.items())
    logger.debug(f"调用 {func_name}({params})")


def log_function_result(func_name: str, result: Any) -> None:
    """记录函数返回值（用于调试）。"""
    if not DEBUG_MODE:
        return
    logger = get_logger("debug.calls")
    logger.debug(f"返回 {func_name} → {_truncate_value(result)}")


def _truncate_value(value: Any, max_len: int = 100) -> str:
    """截断过长的值用于日志显示。"""
    str_val = str(value)
    if len(str_val) > max_len:
        return str_val[:max_len] + "..."
    return str_val

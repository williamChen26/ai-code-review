"""pytest 全局配置。"""

import pytest


@pytest.fixture(autouse=True)
def _suppress_logging() -> None:
    """默认压低日志级别，避免测试输出过多。用 -s 看日志时可忽略。"""
    import logging
    logging.getLogger("app").setLevel(logging.WARNING)

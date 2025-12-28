"""
应用配置加载。

设计目标：
- **严格**：缺少必要环境变量就直接报错（避免“看起来跑了其实没配置好”）
- **类型安全**：使用 Pydantic 校验 URL/字符串等，减少运行时踩坑
- **可测试**：核心加载函数接收 `environ` 显式输入，便于单元测试
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, HttpUrl


class AppConfig(BaseModel):
    """应用运行所需的最小配置集合（全部必填）。"""

    gitlab_base_url: HttpUrl
    gitlab_token: str
    gitlab_webhook_secret: str
    llm_base_url: HttpUrl
    llm_api_key: str
    llm_model: str


def load_config_from_env(environ: Mapping[str, str]) -> AppConfig:
    """
    从环境变量加载并校验配置。

    - **输入**：`environ`（例如 `os.environ`）
    - **输出**：`AppConfig`
    - **失败**：缺失/为空则抛 `ValueError`
    """

    required_keys: tuple[str, ...] = (
        "GITLAB_BASE_URL",
        "GITLAB_TOKEN",
        "GITLAB_WEBHOOK_SECRET",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
    )

    missing: list[str] = [key for key in required_keys if key not in environ or not environ[key]]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    # 交给 Pydantic 做类型校验（例如 URL 合法性）
    return AppConfig(
        gitlab_base_url=environ["GITLAB_BASE_URL"],
        gitlab_token=environ["GITLAB_TOKEN"],
        gitlab_webhook_secret=environ["GITLAB_WEBHOOK_SECRET"],
        llm_base_url=environ["LLM_BASE_URL"],
        llm_api_key=environ["LLM_API_KEY"],
        llm_model=environ["LLM_MODEL"],
    )



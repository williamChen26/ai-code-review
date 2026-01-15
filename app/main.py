"""
FastAPI 服务入口。

这里做三件事：
- 加载配置（严格校验环境变量）
- 组装外部依赖（HTTP Client / LLM Client / GitLab Webhook handler）
- 装配路由（health + gitlab webhook）

注意：
- 业务流程不写在这里（由 `review/orchestrator.py` 负责）
- `httpx.AsyncClient` 会被复用（避免每个请求新建连接）
"""

from __future__ import annotations

import os

import httpx
from fastapi import FastAPI

from app.config import load_config_from_env
from app.github.webhook import build_github_webhook_router
from app.gitlab.webhook import build_gitlab_webhook_router
from app.llm.client import OpenAICompatLLMClient
from app.review.orchestrator import build_review_orchestrator
from app.review.orchestrator import build_github_webhook_handler
from app.review.orchestrator import build_webhook_handler


def build_app() -> FastAPI:
    """创建并返回 FastAPI app（便于测试/复用）。"""

    # 1) 配置：缺失会直接抛错，启动失败（这是期望行为）
    config = load_config_from_env(os.environ)

    # 2) 可复用的 HTTP client：供 GitLab API 与 LLM 调用使用
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    # 3) LLM client：OpenAI-compatible（你后续只要填 base_url/api_key/model）
    llm_client = OpenAICompatLLMClient(
        api_key=config.llm.api_key,
        base_url=str(config.llm.base_url).rstrip("/"),
        http_client=http_client,
        model=config.llm.model,
    )

    # 4) 组装“你写流程，Agent 只负责思考”的 orchestrator
    orchestrator = build_review_orchestrator(llm_client=llm_client)

    app = FastAPI(title="AI Code Review", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        """健康检查：用于 k8s / LB 探活。"""
        return {"status": "ok 2"}

    if config.gitlab is not None:
        gitlab_handler = build_webhook_handler(config=config.gitlab, http_client=http_client, orchestrator=orchestrator)
        app.include_router(build_gitlab_webhook_router(config=config.gitlab, handler=gitlab_handler))
    if config.github is not None:
        github_handler = build_github_webhook_handler(config=config.github, http_client=http_client, orchestrator=orchestrator)
        app.include_router(build_github_webhook_router(config=config.github, handler=github_handler))
    return app


# Uvicorn 默认会从模块级变量 `app` 读取 ASGI 应用
app = build_app()



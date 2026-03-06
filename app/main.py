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

import logging
import os

import httpx
from fastapi import FastAPI

from app.debug_utils import setup_logging
from app.debug_utils import get_logger

# 初始化日志系统 - 设置为 DEBUG 级别以查看所有步骤
setup_logging(level=logging.DEBUG)
logger = get_logger(__name__)

from app.config import load_config_from_env
from app.github.webhook import build_github_webhook_router
from app.gitlab.webhook import build_gitlab_webhook_router
from app.llm.client import LiteLLMClient
from app.review.orchestrator import build_review_orchestrator
from app.review.orchestrator import build_github_webhook_handler
from app.review.orchestrator import build_webhook_handler


def build_app() -> FastAPI:
    """创建并返回 FastAPI app（便于测试/复用）。"""
    logger.info("=" * 60)
    logger.info("AI Code Review 服务启动中...")
    logger.info("=" * 60)

    # 1) 配置：缺失会直接抛错，启动失败（这是期望行为）
    logger.info("Step 1: 加载配置...")
    config = load_config_from_env(os.environ)
    logger.info(f"配置加载成功: GitLab={config.gitlab is not None}, GitHub={config.github is not None}")

    # 2) 可复用的 HTTP client：供 GitLab/GitHub API 调用使用
    logger.info("Step 2: 创建 HTTP Client...")
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    # 3) LLM client：基于 LiteLLM SDK，API Key 从 OPENAI_API_KEY 环境变量自动读取
    logger.info("Step 3: 初始化 LLM Client...")
    llm_client = LiteLLMClient(
        base_url=str(config.llm.base_url).rstrip("/"),
    )
    logger.debug(f"LLM Client 就绪: base_url={config.llm.base_url}")

    # 4) 组装"你写流程，Agent 只负责思考"的 orchestrator
    logger.info("Step 4: 构建 Review Orchestrator...")
    orchestrator = build_review_orchestrator(
        llm_client=llm_client,
        index_storage=config.index_storage,
        embedding=config.embedding,
        repo_sync=config.repo_sync,
    )
    logger.info("Orchestrator 构建完成")

    logger.info("Step 5: 创建 FastAPI 应用...")
    app = FastAPI(title="AI Code Review", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        """健康检查：用于 k8s / LB 探活。"""
        return {"status": "ok 2"}

    if config.gitlab is not None:
        logger.info("Step 6a: 注册 GitLab Webhook 路由...")
        gitlab_handler = build_webhook_handler(config=config.gitlab, http_client=http_client, orchestrator=orchestrator)
        app.include_router(build_gitlab_webhook_router(config=config.gitlab, handler=gitlab_handler))
        logger.info("GitLab Webhook 路由已注册")

    if config.github is not None:
        logger.info("Step 6b: 注册 GitHub Webhook 路由...")
        github_handler = build_github_webhook_handler(config=config.github, http_client=http_client, orchestrator=orchestrator)
        app.include_router(build_github_webhook_router(config=config.github, handler=github_handler))
        logger.info("GitHub Webhook 路由已注册")

    logger.info("=" * 60)
    logger.info("服务启动完成！等待请求...")
    logger.info("=" * 60)
    return app


# Uvicorn 默认会从模块级变量 `app` 读取 ASGI 应用
app = build_app()

"""
FastAPI 服务入口。

职责：
- 加载配置（严格校验环境变量）
- 组装外部依赖（HTTP Client / LLM Client / Webhook handler）
- 装配路由（health + gitlab/github webhook + index/full）

注意：
- 业务流程不写在这里（由 `review/orchestrator.py` 负责）
- `httpx.AsyncClient` 会被复用（避免每个请求新建连接）
"""

from __future__ import annotations

import logging
import os

import anyio
import httpx
from fastapi import FastAPI, Body
from pydantic import BaseModel

from app.debug_utils import setup_logging
from app.debug_utils import get_logger

# 初始化日志系统 - 设置为 DEBUG 级别以查看所有步骤
setup_logging(level=logging.DEBUG)
logger = get_logger(__name__)

from app.config import load_config_from_env
from app.github.webhook import build_github_webhook_router
from app.gitlab.webhook import build_gitlab_webhook_router
from app.indexing.indexer import build_repo_id
from app.indexing.indexer import index_repo_full
from app.llm.client import LiteLLMClient
from app.review.orchestrator import build_review_orchestrator
from app.review.orchestrator import build_github_webhook_handler
from app.review.orchestrator import build_webhook_handler


class IndexFullRequest(BaseModel):
    """手动触发全量索引的请求体。"""
    provider: str  # "github" | "gitlab"
    repo_key: str  # e.g. "owner/repo" or "project_id"
    clone_url: str  # git clone URL
    branch: str = "main"  # 索引分支
    token: str | None = None
    token_user: str | None = None


class IndexLocalRequest(BaseModel):
    """手动触发本地目录索引（调试用，无需 clone）。"""
    provider: str
    repo_key: str
    repo_dir: str


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
        return {"status": "ok"}

    # ---- 手动全量索引端点 ----

    @app.post("/index/full")
    async def index_full(req: IndexFullRequest = Body(...)) -> dict[str, str]:
        """手动触发全量索引（用于调试和初始化）。

        会先 clone/pull 仓库到本地，然后执行：
        AST 解析 → 写 files/symbols → 生成 embedding → 写 embeddings
        """
        logger.info(f"手动全量索引: provider={req.provider}, repo_key={req.repo_key}")
        repo_id = build_repo_id(provider=req.provider, repo_key=req.repo_key)
        async with orchestrator.lock_manager.acquire(repo_id):
            repo_dir = await anyio.to_thread.run_sync(
                lambda: orchestrator.repo_syncer.ensure_repo(
                    repo_id=repo_id,
                    clone_url=req.clone_url,
                    target_branch=req.branch,
                    token=req.token,
                    token_user=req.token_user,
                )
            )
            await index_repo_full(
                storage_client=orchestrator.storage_client,
                embedding_api_base=orchestrator.embedding_api_base,
                repo_id=repo_id,
                repo_dir=repo_dir,
            )
        logger.info(f"全量索引完成: repo_id={repo_id}")
        return {"status": "ok", "repo_id": repo_id}

    @app.post("/index/local")
    async def index_local(req: IndexLocalRequest = Body(...)) -> dict[str, str]:
        """直接对本地目录做全量索引（调试用，省去 git clone）。

        适合本地测试：先手动 clone 一个项目到本地，然后用这个端点触发索引，
        观察日志中的 chunk 进度、embedding 调用、DB 写入。

        示例：
        curl -X POST http://localhost:8000/index/local \\
          -H 'Content-Type: application/json' \\
          -d '{"provider":"local","repo_key":"my-project","repo_dir":"/path/to/repo"}'
        """
        import time
        t0 = time.monotonic()
        repo_id = build_repo_id(provider=req.provider, repo_key=req.repo_key)
        logger.info(f"本地索引开始: repo_id={repo_id}, repo_dir={req.repo_dir}")

        async with orchestrator.lock_manager.acquire(repo_id):
            await index_repo_full(
                storage_client=orchestrator.storage_client,
                embedding_api_base=orchestrator.embedding_api_base,
                repo_id=repo_id,
                repo_dir=req.repo_dir,
            )

        elapsed = time.monotonic() - t0
        logger.info(f"本地索引完成: repo_id={repo_id}, 耗时={elapsed:.1f}s")
        return {"status": "ok", "repo_id": repo_id, "elapsed_seconds": f"{elapsed:.1f}"}

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

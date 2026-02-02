"""
GitLab Webhook 接入层。

职责：
- 校验 `X-Gitlab-Token`（防止被随意调用）
- 解析 webhook payload -> Pydantic schema（类型安全）
- 过滤掉不关心的事件（只处理 MR open/update/reopen）
- 调用业务 handler（真正的 review 流程在 orchestrator 里）
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi import Header
from fastapi import HTTPException
from fastapi import Request

from app.config import GitLabConfig
from app.gitlab.schemas import GitLabMergeRequestWebhookEvent

WebhookHandler = Callable[[GitLabMergeRequestWebhookEvent], Awaitable[None]]


def build_gitlab_webhook_router(config: GitLabConfig, handler: WebhookHandler) -> APIRouter:
    """创建 GitLab webhook 路由。"""
    router = APIRouter()

    @router.post("/gitlab/webhook")
    async def gitlab_webhook(
        request: Request,
        x_gitlab_token: str = Header(alias="X-Gitlab-Token"),
    ) -> dict[str, str]:
        # 1) Webhook secret 校验（GitLab UI 里配置）
        if x_gitlab_token != config.webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook token")

        # 2) 解析 payload（不做 try/except：出错就直接 4xx，便于发现问题）
        payload = await request.json()
        event = GitLabMergeRequestWebhookEvent.model_validate(payload)
        if event.object_kind != "merge_request":
            return {"status": "ignored"}

        # 3) 只处理我们关心的 MR 动作
        if event.object_attributes.action not in ("open", "update", "reopen", "merge"):
            return {"status": "ignored"}

        # 4) 交给业务 handler（由 orchestrator 装配）
        await handler(event)
        return {"status": "ok"}

    return router



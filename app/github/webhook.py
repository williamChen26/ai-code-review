"""
GitHub Webhook 接入层。

职责：
- 校验 `X-Hub-Signature-256`（HMAC SHA256）
- 校验 event 类型（只处理 pull_request）
- 解析 payload -> Pydantic schema
- 过滤 action（opened/reopened/synchronize）
- 调用业务 handler
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi import Header
from fastapi import HTTPException
from fastapi import Request

from app.config import GitHubConfig
from app.github.schemas import GitHubPullRequestWebhookEvent

GitHubWebhookHandler = Callable[[GitHubPullRequestWebhookEvent], Awaitable[None]]


def _verify_github_signature(body: bytes, signature_header: str, secret: str) -> None:
    if not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Invalid signature header")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def build_github_webhook_router(config: GitHubConfig, handler: GitHubWebhookHandler) -> APIRouter:
    router = APIRouter()

    @router.post("/github/webhook")
    async def github_webhook(
        request: Request,
        x_github_event: str = Header(alias="X-GitHub-Event"),
        x_hub_signature_256: str = Header(alias="X-Hub-Signature-256"),
    ) -> dict[str, str]:
        if x_github_event != "pull_request":
            return {"status": "ignored"}

        body = await request.body()
        _verify_github_signature(body=body, signature_header=x_hub_signature_256, secret=config.webhook_secret)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        event = GitHubPullRequestWebhookEvent.model_validate(payload)
        if event.action not in ("opened", "reopened", "synchronize"):
            return {"status": "ignored"}

        await handler(event)
        return {"status": "ok"}

    return router



"""
Review Orchestrator（核心流程编排）。

关键思想（你强调的那点）：
- **流程由工程代码控制**：明确的 4 阶段 pipeline
- **LLM 只负责“思考/生成结构化输出”**：planner 单次，reviewer 可扩展为受控 ReAct

目前的最小闭环：
Webhook -> get MR changes -> build context -> plan risk -> review -> synthesize -> post MR note
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from app.config import AppConfig
from app.gitlab.client import GitLabClient
from app.gitlab.schemas import GitLabMergeRequestWebhookEvent
from app.llm.client import OpenAICompatLLMClient
from app.review.context import build_merge_request_context
from app.review.planner import plan_risk
from app.review.reviewer import review_high_risk_files
from app.review.synthesis import synthesize_gitlab_note_body


@dataclass(frozen=True)
class ReviewOrchestrator:
    """Orchestrator 运行时依赖集合（目前只需要 LLM client）。"""

    llm_client: OpenAICompatLLMClient


def build_review_orchestrator(llm_client: OpenAICompatLLMClient) -> ReviewOrchestrator:
    """创建 orchestrator（便于未来注入 cache/queue 等依赖）。"""
    return ReviewOrchestrator(llm_client=llm_client)


async def run_review(
    orchestrator: ReviewOrchestrator,
    gitlab_client: GitLabClient,
    project_id: int,
    mr_iid: int,
    head_sha: str,
) -> str:
    """
    跑一次完整 review，返回最终要写回 GitLab 的评论正文。

    4 阶段：
    - Step 1: Collect Context（非 AI）
    - Step 2: Risk Planning（LLM 单次 JSON）
    - Step 3: Focused Review（文件级；当前实现为逐文件 JSON 输出）
    - Step 4: Synthesize（确定性拼接/或后续可换成 LLM 无 loop）
    """
    changes = await gitlab_client.get_merge_request_changes(project_id=project_id, mr_iid=mr_iid)
    context = build_merge_request_context(
        project_id=project_id,
        mr_iid=mr_iid,
        head_sha=head_sha,
        changes=changes,
    )

    plan = await plan_risk(llm_client=orchestrator.llm_client, context=context)
    comments = await review_high_risk_files(
        llm_client=orchestrator.llm_client,
        changes=context.changes,
        plan=plan,
    )
    return synthesize_gitlab_note_body(head_sha=head_sha, plan=plan, comments=comments)


def build_webhook_handler(
    config: AppConfig,
    http_client: httpx.AsyncClient,
    orchestrator: ReviewOrchestrator,
) -> Callable[[GitLabMergeRequestWebhookEvent], Awaitable[None]]:
    """
    装配 webhook handler：
    - 把外部依赖（GitLabClient）和业务编排（orchestrator）绑定起来
    - 返回一个 `async def handle(event)` 给 webhook 路由调用
    """
    gitlab_client = GitLabClient(
        base_url=str(config.gitlab_base_url).rstrip("/"),
        private_token=config.gitlab_token,
        http_client=http_client,
    )

    async def handle(event: GitLabMergeRequestWebhookEvent) -> None:
        """处理单次 MR webhook：跑 review，并把结果写回 GitLab。"""
        project_id = event.project.id
        mr_iid = event.object_attributes.iid
        last_commit = event.object_attributes.last_commit
        head_sha_obj = last_commit.get("id")
        if not isinstance(head_sha_obj, str) or not head_sha_obj:
            raise ValueError("Webhook payload missing object_attributes.last_commit.id")

        note_body = await run_review(
            orchestrator=orchestrator,
            gitlab_client=gitlab_client,
            project_id=project_id,
            mr_iid=mr_iid,
            head_sha=head_sha_obj,
        )
        await gitlab_client.post_merge_request_note(project_id=project_id, mr_iid=mr_iid, body=note_body)

    return handle



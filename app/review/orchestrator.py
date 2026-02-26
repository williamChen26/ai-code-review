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

from app.config import EmbeddingConfig
from app.config import GitHubConfig
from app.config import GitLabConfig
from app.config import IndexStorageConfig
from app.config import RepoSyncConfig
from app.gitlab.adapter import build_review_context_from_gitlab_changes
from app.gitlab.client import GitLabClient
from app.gitlab.schemas import GitLabMergeRequestChanges
from app.gitlab.schemas import GitLabMergeRequestWebhookEvent
from app.github.schemas import GitHubPullRequestFile
from app.github.schemas import GitHubPullRequestWebhookEvent
from app.indexing.indexer import build_repo_id
from app.indexing.indexer import ensure_initial_index
from app.indexing.indexer import index_repo_incremental
from app.indexing.repo_sync import RepoSyncer
from app.llm.client import OpenAICompatLLMClient
from app.review.context_retrieval import build_context_package_for_change
from app.review.models import ReviewContext
from app.review.planner import plan_risk
from app.review.reviewer import review_high_risk_files
from app.review.synthesis import synthesize_review_markdown_body
from app.storage.pg import IndexStorageClient
from app.storage.pg import ensure_schema


@dataclass(frozen=True)
class ReviewOrchestrator:
    """Orchestrator 运行时依赖集合（目前只需要 LLM client）。"""

    llm_client: OpenAICompatLLMClient
    storage_client: IndexStorageClient
    repo_syncer: RepoSyncer
    embedding_model: str
    repo_clone_url: str


def build_review_orchestrator(
    llm_client: OpenAICompatLLMClient,
    index_storage: IndexStorageConfig,
    embedding: EmbeddingConfig,
    repo_sync: RepoSyncConfig,
) -> ReviewOrchestrator:
    """创建 orchestrator（便于未来注入 cache/queue 等依赖）。"""
    storage_client = IndexStorageClient(dsn=index_storage.dsn, embedding_dim=embedding.dimension)
    ensure_schema(storage_client)
    repo_syncer = RepoSyncer(base_dir=repo_sync.base_dir, git_bin=repo_sync.git_bin)
    return ReviewOrchestrator(
        llm_client=llm_client,
        storage_client=storage_client,
        repo_syncer=repo_syncer,
        embedding_model=embedding.model,
        repo_clone_url=repo_sync.clone_url,
    )


async def run_review(
    orchestrator: ReviewOrchestrator,
    context: ReviewContext,
) -> str:
    """
    跑一次完整 review，返回最终要写回 GitLab 的评论正文。

    4 阶段：
    - Step 1: Collect Context（非 AI）
    - Step 2: Risk Planning（LLM 单次 JSON）
    - Step 3: Focused Review（文件级；当前实现为逐文件 JSON 输出）
    - Step 4: Synthesize（确定性拼接/或后续可换成 LLM 无 loop）
    """
    plan = await plan_risk(llm_client=orchestrator.llm_client, context=context)
    repo_id = context.repo_id
    context_by_path: dict[str, str] = {}
    for change in context.changes:
        context_by_path[change.path] = await build_context_package_for_change(
            storage_client=orchestrator.storage_client,
            llm_client=orchestrator.llm_client,
            embedding_model=orchestrator.embedding_model,
            repo_id=repo_id,
            file_change=change,
        )
    comments = await review_high_risk_files(
        llm_client=orchestrator.llm_client,
        changes=context.changes,
        plan=plan,
        context_by_path=context_by_path,
    )
    return synthesize_review_markdown_body(head_sha=context.head_sha, plan=plan, comments=comments)


def build_webhook_handler(
    config: GitLabConfig,
    http_client: httpx.AsyncClient,
    orchestrator: ReviewOrchestrator,
) -> Callable[[GitLabMergeRequestWebhookEvent], Awaitable[None]]:
    """
    装配 webhook handler：
    - 把外部依赖（GitLabClient）和业务编排（orchestrator）绑定起来
    - 返回一个 `async def handle(event)` 给 webhook 路由调用
    """
    gitlab_client = GitLabClient(
        base_url=str(config.base_url).rstrip("/"),
        private_token=config.token,
        http_client=http_client,
    )

    async def handle(event: GitLabMergeRequestWebhookEvent) -> None:
        """处理单次 MR webhook：按 action 跑 review 或增量索引。"""
        project_id = event.project.id
        mr_iid = event.object_attributes.iid
        last_commit = event.object_attributes.last_commit
        head_sha_obj = last_commit.get("id")
        if not isinstance(head_sha_obj, str) or not head_sha_obj:
            raise ValueError("Webhook payload missing object_attributes.last_commit.id")

        changes = await gitlab_client.get_merge_request_changes(project_id=project_id, mr_iid=mr_iid)
        if event.object_attributes.action == "merge":
            await _handle_gitlab_merge_indexing(
                orchestrator=orchestrator,
                config=config,
                event=event,
                changes=changes,
            )
            return
        repo_id = build_repo_id(provider="gitlab", repo_key=str(event.project.id))
        index_branch = _resolve_index_branch(target_branch=event.object_attributes.target_branch)
        repo_dir = orchestrator.repo_syncer.ensure_repo(
            repo_id=repo_id,
            clone_url=orchestrator.repo_clone_url,
            target_branch=index_branch,
            token=config.token,
            token_user="oauth2",
        )
        await ensure_initial_index(
            storage_client=orchestrator.storage_client,
            llm_client=orchestrator.llm_client,
            embedding_model=orchestrator.embedding_model,
            repo_id=repo_id,
            repo_dir=repo_dir,
        )
        context = build_review_context_from_gitlab_changes(
            project_id=project_id,
            mr_iid=mr_iid,
            head_sha=head_sha_obj,
            changes=changes,
        )
        note_body = await run_review(orchestrator=orchestrator, context=context)
        await gitlab_client.post_merge_request_note(project_id=project_id, mr_iid=mr_iid, body=note_body)

    return handle


def build_github_webhook_handler(
    config: GitHubConfig,
    http_client: httpx.AsyncClient,
    orchestrator: ReviewOrchestrator,
) -> Callable[[GitHubPullRequestWebhookEvent], Awaitable[None]]:
    """
    装配 GitHub webhook handler：
    - 拉取 PR files/patch
    - 跑 review pipeline
    - 写回 GitHub PR review（event=COMMENT）
    """
    from app.github.adapter import build_review_context_from_github_pull_request_files
    from app.github.client import GitHubClient
    github_client = GitHubClient(api_base_url=str(config.api_base_url).rstrip("/"), token=config.token, http_client=http_client)

    async def handle(event: GitHubPullRequestWebhookEvent) -> None:
        owner = event.repository.owner.login
        repo = event.repository.name
        pull_number = event.pull_request.number
        head_sha = event.pull_request.head.sha

        files = await github_client.list_pull_request_files(owner=owner, repo=repo, pull_number=pull_number)
        if event.action == "closed" and event.pull_request.merged:
            await _handle_github_merge_indexing(
                orchestrator=orchestrator,
                config=config,
                event=event,
                files=files,
            )
            return
        repo_id = build_repo_id(provider="github", repo_key=event.repository.full_name)
        index_branch = _resolve_index_branch(target_branch=event.pull_request.base.ref)
        repo_dir = orchestrator.repo_syncer.ensure_repo(
            repo_id=repo_id,
            clone_url=orchestrator.repo_clone_url,
            target_branch=index_branch,
            token=config.token,
            token_user="x-access-token",
        )
        await ensure_initial_index(
            storage_client=orchestrator.storage_client,
            llm_client=orchestrator.llm_client,
            embedding_model=orchestrator.embedding_model,
            repo_id=repo_id,
            repo_dir=repo_dir,
        )
        context = build_review_context_from_github_pull_request_files(
            owner=owner,
            repo=repo,
            pull_number=pull_number,
            head_sha=head_sha,
            files=files,
        )
        body = await run_review(orchestrator=orchestrator, context=context)
        await github_client.create_pull_request_review(
            owner=owner,
            repo=repo,
            pull_number=pull_number,
            commit_id=context.head_sha,
            body=body,
        )

    return handle


def _resolve_index_branch(target_branch: str) -> str:
    if target_branch == "main":
        return target_branch
    return "main"


async def _handle_gitlab_merge_indexing(
    orchestrator: ReviewOrchestrator,
    config: GitLabConfig,
    event: GitLabMergeRequestWebhookEvent,
    changes: GitLabMergeRequestChanges,
) -> None:
    target_branch = event.object_attributes.target_branch
    if target_branch != "main":
        return
    repo_id = build_repo_id(provider="gitlab", repo_key=str(event.project.id))
    repo_dir = orchestrator.repo_syncer.ensure_repo(
        repo_id=repo_id,
        clone_url=orchestrator.repo_clone_url,
        target_branch=target_branch,
        token=config.token,
        token_user="oauth2",
    )
    changed_paths = [c.new_path for c in changes.changes if not c.deleted_file]
    deleted_paths = [c.new_path for c in changes.changes if c.deleted_file]
    initial_built = await ensure_initial_index(
        storage_client=orchestrator.storage_client,
        llm_client=orchestrator.llm_client,
        embedding_model=orchestrator.embedding_model,
        repo_id=repo_id,
        repo_dir=repo_dir,
    )
    if not initial_built:
        await index_repo_incremental(
            storage_client=orchestrator.storage_client,
            llm_client=orchestrator.llm_client,
            embedding_model=orchestrator.embedding_model,
            repo_id=repo_id,
            repo_dir=repo_dir,
            changed_paths=changed_paths,
            deleted_paths=deleted_paths,
        )


async def _handle_github_merge_indexing(
    orchestrator: ReviewOrchestrator,
    config: GitHubConfig,
    event: GitHubPullRequestWebhookEvent,
    files: list[GitHubPullRequestFile],
) -> None:
    target_branch = event.pull_request.base.ref
    if target_branch != "main":
        return
    repo_id = build_repo_id(provider="github", repo_key=event.repository.full_name)
    repo_dir = orchestrator.repo_syncer.ensure_repo(
        repo_id=repo_id,
        clone_url=orchestrator.repo_clone_url,
        target_branch=target_branch,
        token=config.token,
        token_user="x-access-token",
    )
    changed_paths = [f.filename for f in files if f.status != "removed"]
    deleted_paths = [f.filename for f in files if f.status == "removed"]
    initial_built = await ensure_initial_index(
        storage_client=orchestrator.storage_client,
        llm_client=orchestrator.llm_client,
        embedding_model=orchestrator.embedding_model,
        repo_id=repo_id,
        repo_dir=repo_dir,
    )
    if not initial_built:
        await index_repo_incremental(
            storage_client=orchestrator.storage_client,
            llm_client=orchestrator.llm_client,
            embedding_model=orchestrator.embedding_model,
            repo_id=repo_id,
            repo_dir=repo_dir,
            changed_paths=changed_paths,
            deleted_paths=deleted_paths,
        )

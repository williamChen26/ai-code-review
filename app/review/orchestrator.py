"""
Review Orchestrator（核心流程编排）。

关键思想：
- **流程由工程代码控制**：明确的 pipeline 阶段
- **LLM 只负责"思考/生成结构化输出"**：planner 单次，reviewer 逐文件

流程：
Webhook -> get changes -> sync repo -> ensure index
       -> diff → changed symbols → related symbols → context
       -> plan risk -> review -> synthesize -> post comment
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
from app.llm.client import LiteLLMClient
from app.review.context_retrieval import ReviewContextPackage
from app.review.context_retrieval import build_context_package_for_change
from app.review.models import ReviewContext
from app.review.planner import plan_risk
from app.review.reviewer import review_high_risk_files
from app.review.synthesis import synthesize_review_markdown_body
from app.storage.pg import IndexStorageClient
from app.storage.pg import ensure_schema
from app.debug_utils import get_logger
from app.debug_utils import step_tracker

logger = get_logger(__name__)


@dataclass(frozen=True)
class ReviewOrchestrator:
    """Orchestrator 运行时依赖集合。

    - llm_client: 用于聊天补全（risk planning / file review）
    - storage_client: 索引/向量库客户端
    - repo_syncer: git clone/pull 管理
    - embedding_api_base: LiteLLM Proxy 地址
    """

    llm_client: LiteLLMClient
    storage_client: IndexStorageClient
    repo_syncer: RepoSyncer
    embedding_api_base: str


def build_review_orchestrator(
    llm_client: LiteLLMClient,
    index_storage: IndexStorageConfig,
    embedding: EmbeddingConfig,
    repo_sync: RepoSyncConfig,
) -> ReviewOrchestrator:
    """创建 orchestrator（便于未来注入 cache/queue 等依赖）。"""
    storage_client = IndexStorageClient(dsn=index_storage.dsn)
    ensure_schema(storage_client)
    repo_syncer = RepoSyncer(base_dir=repo_sync.base_dir, git_bin=repo_sync.git_bin)
    return ReviewOrchestrator(
        llm_client=llm_client,
        storage_client=storage_client,
        repo_syncer=repo_syncer,
        embedding_api_base=embedding.api_base,
    )


async def run_review(
    orchestrator: ReviewOrchestrator,
    context: ReviewContext,
) -> str:
    """跑一次完整 review，返回最终要写回 GitLab/GitHub 的评论正文。

    Pipeline：
    1. Risk Planning（LLM 单次 JSON）
    2. 构建结构化上下文（diff → changed symbols → related symbols）
    3. Focused Review（文件级 LLM JSON 输出）
    4. Synthesize（确定性拼接）
    """
    with step_tracker("run_review") as tracker:
        tracker.step(f"开始 Review, 共 {len(context.changes)} 个变更文件")
        logger.debug(f"变更文件: {[c.path for c in context.changes]}")

        # Step 1: Risk Planning
        tracker.step("Risk Planning - 调用 LLM 分析风险")
        plan = await plan_risk(llm_client=orchestrator.llm_client, context=context)
        logger.info(f"Risk Plan 结果: highRiskFiles={plan.highRiskFiles}, depth={plan.reviewDepth}")

        # Step 2: 构建结构化 symbol 上下文
        tracker.step("构建上下文包 - 检索 changed/related symbols")
        repo_id = context.repo_id
        context_by_path: dict[str, ReviewContextPackage] = {}
        for i, change in enumerate(context.changes):
            tracker.substep(f"处理文件 [{i+1}/{len(context.changes)}]: {change.path}")
            context_by_path[change.path] = await build_context_package_for_change(
                storage_client=orchestrator.storage_client,
                embedding_api_base=orchestrator.embedding_api_base,
                repo_id=repo_id,
                file_change=change,
            )
            pkg = context_by_path[change.path]
            logger.debug(
                f"  {change.path}: "
                f"changed_symbols={len(pkg.changed_symbols)}, "
                f"related_symbols={len(pkg.related_symbols)}"
            )

        # Step 3: 文件级 Review
        tracker.step(f"文件级 Review - 审查 {len(plan.highRiskFiles)} 个高风险文件")
        comments = await review_high_risk_files(
            llm_client=orchestrator.llm_client,
            changes=context.changes,
            plan=plan,
            context_by_path=context_by_path,
        )
        logger.info(f"Review 生成了 {len(comments)} 条评论")

        # Step 4: 合成最终评论
        tracker.step("合成最终评论")
        result = synthesize_review_markdown_body(
            head_sha=context.head_sha, plan=plan, comments=comments,
        )
        logger.debug(f"最终评论长度: {len(result)} 字符")

        return result


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
        with step_tracker("gitlab_webhook") as tracker:
            tracker.step("解析 Webhook Event")
            project_id = event.project.id
            mr_iid = event.object_attributes.iid
            last_commit = event.object_attributes.last_commit
            head_sha_obj = last_commit.get("id")
            logger.info(f"GitLab MR Webhook: project={project_id}, mr_iid={mr_iid}, action={event.object_attributes.action}")

            if not isinstance(head_sha_obj, str) or not head_sha_obj:
                raise ValueError("Webhook payload missing object_attributes.last_commit.id")

            tracker.step("获取 MR 变更列表")
            changes = await gitlab_client.get_merge_request_changes(project_id=project_id, mr_iid=mr_iid)
            logger.info(f"获取到 {len(changes.changes)} 个变更文件")

            if event.object_attributes.action == "merge":
                tracker.step("处理合并事件 - 增量索引")
                await _handle_gitlab_merge_indexing(
                    orchestrator=orchestrator,
                    config=config,
                    event=event,
                    changes=changes,
                )
                return

            tracker.step("同步仓库")
            repo_id = build_repo_id(provider="gitlab", repo_key=str(event.project.id))
            clone_url = event.project.git_http_url
            index_branch = _resolve_index_branch(target_branch=event.object_attributes.target_branch)
            logger.debug(f"repo_id={repo_id}, clone_url={clone_url}, index_branch={index_branch}")
            repo_dir = orchestrator.repo_syncer.ensure_repo(
                repo_id=repo_id,
                clone_url=clone_url,
                target_branch=index_branch,
                token=config.token,
                token_user="oauth2",
            )
            logger.debug(f"仓库同步完成: {repo_dir}")

            tracker.step("确保初始索引存在")
            await ensure_initial_index(
                storage_client=orchestrator.storage_client,
                embedding_api_base=orchestrator.embedding_api_base,
                repo_id=repo_id,
                repo_dir=repo_dir,
            )

            tracker.step("构建 Review Context")
            context = build_review_context_from_gitlab_changes(
                project_id=project_id,
                mr_iid=mr_iid,
                head_sha=head_sha_obj,
                changes=changes,
            )

            tracker.step("执行 AI Review")
            note_body = await run_review(orchestrator=orchestrator, context=context)

            tracker.step("发送评论到 GitLab MR")
            await gitlab_client.post_merge_request_note(project_id=project_id, mr_iid=mr_iid, body=note_body)
            logger.info("评论发送成功")

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
        with step_tracker("github_webhook") as tracker:
            tracker.step("解析 Webhook Event")
            owner = event.repository.owner.login
            repo = event.repository.name
            pull_number = event.pull_request.number
            head_sha = event.pull_request.head.sha
            logger.info(f"GitHub PR Webhook: {owner}/{repo}#{pull_number}, action={event.action}")

            tracker.step("获取 PR 变更文件列表")
            files = await github_client.list_pull_request_files(owner=owner, repo=repo, pull_number=pull_number)
            logger.info(f"获取到 {len(files)} 个变更文件")

            if event.action == "closed" and event.pull_request.merged:
                tracker.step("处理合并事件 - 增量索引")
                await _handle_github_merge_indexing(
                    orchestrator=orchestrator,
                    config=config,
                    event=event,
                    files=files,
                )
                return

            tracker.step("同步仓库")
            repo_id = build_repo_id(provider="github", repo_key=event.repository.full_name)
            clone_url = event.repository.clone_url
            index_branch = _resolve_index_branch(target_branch=event.pull_request.base.ref)
            logger.debug(f"repo_id={repo_id}, clone_url={clone_url}, index_branch={index_branch}")
            repo_dir = orchestrator.repo_syncer.ensure_repo(
                repo_id=repo_id,
                clone_url=clone_url,
                target_branch=index_branch,
                token=config.token,
                token_user="x-access-token",
            )
            logger.debug(f"仓库同步完成: {repo_dir}")

            tracker.step("确保初始索引存在")
            await ensure_initial_index(
                storage_client=orchestrator.storage_client,
                embedding_api_base=orchestrator.embedding_api_base,
                repo_id=repo_id,
                repo_dir=repo_dir,
            )

            tracker.step("构建 Review Context")
            context = build_review_context_from_github_pull_request_files(
                owner=owner,
                repo=repo,
                pull_number=pull_number,
                head_sha=head_sha,
                files=files,
            )

            tracker.step("执行 AI Review")
            body = await run_review(orchestrator=orchestrator, context=context)

            tracker.step("发送 Review 到 GitHub PR")
            await github_client.create_pull_request_review(
                owner=owner,
                repo=repo,
                pull_number=pull_number,
                commit_id=context.head_sha,
                body=body,
            )
            logger.info("Review 发送成功")

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
        clone_url=event.project.git_http_url,
        target_branch=target_branch,
        token=config.token,
        token_user="oauth2",
    )
    changed_paths = [c.new_path for c in changes.changes if not c.deleted_file]
    deleted_paths = [c.new_path for c in changes.changes if c.deleted_file]
    initial_built = await ensure_initial_index(
        storage_client=orchestrator.storage_client,
        embedding_api_base=orchestrator.embedding_api_base,
        repo_id=repo_id,
        repo_dir=repo_dir,
    )
    if not initial_built:
        await index_repo_incremental(
            storage_client=orchestrator.storage_client,
            embedding_api_base=orchestrator.embedding_api_base,
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
        clone_url=event.repository.clone_url,
        target_branch=target_branch,
        token=config.token,
        token_user="x-access-token",
    )
    changed_paths = [f.filename for f in files if f.status != "removed"]
    deleted_paths = [f.filename for f in files if f.status == "removed"]
    initial_built = await ensure_initial_index(
        storage_client=orchestrator.storage_client,
        embedding_api_base=orchestrator.embedding_api_base,
        repo_id=repo_id,
        repo_dir=repo_dir,
    )
    if not initial_built:
        await index_repo_incremental(
            storage_client=orchestrator.storage_client,
            embedding_api_base=orchestrator.embedding_api_base,
            repo_id=repo_id,
            repo_dir=repo_dir,
            changed_paths=changed_paths,
            deleted_paths=deleted_paths,
        )

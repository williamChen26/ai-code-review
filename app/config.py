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


class GitLabConfig(BaseModel):
    """GitLab 集成配置（启用 GitLab webhook 时必填）。"""

    base_url: HttpUrl
    token: str
    webhook_secret: str


class GitHubConfig(BaseModel):
    """GitHub 集成配置（启用 GitHub webhook 时必填）。"""

    api_base_url: HttpUrl
    token: str
    webhook_secret: str


class LLMConfig(BaseModel):
    """LLM 配置（任何评审都需要）。"""

    base_url: HttpUrl
    api_key: str
    model: str


class EmbeddingConfig(BaseModel):
    """Embedding 配置（用于向量库与上下文检索）。"""

    model: str
    dimension: int


class IndexStorageConfig(BaseModel):
    """索引/向量库配置（Postgres + pgvector）。"""

    dsn: str


class RepoSyncConfig(BaseModel):
    """仓库同步配置（git clone/pull）。"""

    base_dir: str
    git_bin: str


class AppConfig(BaseModel):
    """应用配置：LLM 必填；GitLab/GitHub 至少启用一个。"""

    llm: LLMConfig
    embedding: EmbeddingConfig
    index_storage: IndexStorageConfig
    repo_sync: RepoSyncConfig
    gitlab: GitLabConfig | None
    github: GitHubConfig | None


def _load_optional_group(environ: Mapping[str, str], keys: tuple[str, ...], group_name: str) -> dict[str, str] | None:
    """
    从 environ 加载一组“可选但必须成组完整”的配置。

    - 如果 keys 全部缺失/为空：返回 None（表示该集成未启用）
    - 如果 keys 部分存在：抛 ValueError（避免“看似启用但其实缺配置”）
    - 如果 keys 全部存在且非空：返回 dict
    """
    values: dict[str, str] = {k: environ.get(k, "") for k in keys}
    present = {k for k, v in values.items() if v}
    if not present:
        return None
    missing = [k for k in keys if not values.get(k)]
    if missing:
        raise ValueError(f"Missing required env vars for {group_name}: {', '.join(missing)}")
    return values


def _load_required_group(environ: Mapping[str, str], keys: tuple[str, ...], group_name: str) -> dict[str, str]:
    values: dict[str, str] = {k: environ.get(k, "") for k in keys}
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise ValueError(f"Missing required env vars for {group_name}: {', '.join(missing)}")
    return values


def load_config_from_env(environ: Mapping[str, str]) -> AppConfig:
    """
    从环境变量加载并校验配置。

    - **输入**：`environ`（例如 `os.environ`）
    - **输出**：`AppConfig`
    - **失败**：缺失/为空则抛 `ValueError`
    """

    llm_required: tuple[str, ...] = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    llm_missing: list[str] = [key for key in llm_required if key not in environ or not environ[key]]
    if llm_missing:
        raise ValueError(f"Missing required env vars: {', '.join(llm_missing)}")

    indexing_raw = _load_required_group(
        environ=environ,
        keys=(
            "INDEX_PG_DSN",
            "INDEX_EMBED_MODEL",
            "INDEX_EMBED_DIM",
            "INDEX_REPO_BASE_DIR",
            "INDEX_GIT_BIN",
        ),
        group_name="indexing",
    )

    gitlab_raw = _load_optional_group(
        environ=environ,
        keys=("GITLAB_BASE_URL", "GITLAB_TOKEN", "GITLAB_WEBHOOK_SECRET"),
        group_name="gitlab",
    )
    github_raw = _load_optional_group(
        environ=environ,
        keys=("GITHUB_API_BASE_URL", "GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET"),
        group_name="github",
    )
    if gitlab_raw is None and github_raw is None:
        raise ValueError("At least one SCM integration must be configured: GitLab or GitHub")

    return AppConfig(
        llm=LLMConfig(base_url=environ["LLM_BASE_URL"], api_key=environ["LLM_API_KEY"], model=environ["LLM_MODEL"]),
        embedding=EmbeddingConfig(
            model=indexing_raw["INDEX_EMBED_MODEL"],
            dimension=int(indexing_raw["INDEX_EMBED_DIM"]),
        ),
        index_storage=IndexStorageConfig(dsn=indexing_raw["INDEX_PG_DSN"]),
        repo_sync=RepoSyncConfig(
            base_dir=indexing_raw["INDEX_REPO_BASE_DIR"],
            git_bin=indexing_raw["INDEX_GIT_BIN"],
        ),
        gitlab=(
            None
            if gitlab_raw is None
            else GitLabConfig(
                base_url=gitlab_raw["GITLAB_BASE_URL"],
                token=gitlab_raw["GITLAB_TOKEN"],
                webhook_secret=gitlab_raw["GITLAB_WEBHOOK_SECRET"],
            )
        ),
        github=(
            None
            if github_raw is None
            else GitHubConfig(
                api_base_url=github_raw["GITHUB_API_BASE_URL"],
                token=github_raw["GITHUB_TOKEN"],
                webhook_secret=github_raw["GITHUB_WEBHOOK_SECRET"],
            )
        ),
    )



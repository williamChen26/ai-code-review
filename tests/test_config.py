from __future__ import annotations

import pytest

from app.config import load_config_from_env


def test_load_config_requires_llm() -> None:
    with pytest.raises(ValueError):
        load_config_from_env(environ={})


def test_load_config_requires_at_least_one_scm() -> None:
    environ = {
        "LLM_BASE_URL": "https://llm.example.com",
        "LLM_API_KEY": "k",
        "LLM_MODEL": "m",
        "INDEX_PG_DSN": "postgresql://user:pass@localhost:5432/db",
        "INDEX_EMBED_MODEL": "text-embedding-3-large",
        "INDEX_EMBED_DIM": "3072",
        "INDEX_REPO_BASE_DIR": "/tmp/repos",
        "INDEX_GIT_BIN": "git",
    }
    with pytest.raises(ValueError):
        load_config_from_env(environ=environ)


def test_load_config_gitlab_only_ok() -> None:
    environ = {
        "LLM_BASE_URL": "https://llm.example.com",
        "LLM_API_KEY": "k",
        "LLM_MODEL": "m",
        "INDEX_PG_DSN": "postgresql://user:pass@localhost:5432/db",
        "INDEX_EMBED_MODEL": "text-embedding-3-large",
        "INDEX_EMBED_DIM": "3072",
        "INDEX_REPO_BASE_DIR": "/tmp/repos",
        "INDEX_GIT_BIN": "git",
        "GITLAB_BASE_URL": "https://gitlab.example.com",
        "GITLAB_TOKEN": "t",
        "GITLAB_WEBHOOK_SECRET": "s",
    }
    cfg = load_config_from_env(environ=environ)
    assert cfg.gitlab is not None
    assert cfg.github is None


def test_load_config_github_only_ok() -> None:
    environ = {
        "LLM_BASE_URL": "https://llm.example.com",
        "LLM_API_KEY": "k",
        "LLM_MODEL": "m",
        "INDEX_PG_DSN": "postgresql://user:pass@localhost:5432/db",
        "INDEX_EMBED_MODEL": "text-embedding-3-large",
        "INDEX_EMBED_DIM": "3072",
        "INDEX_REPO_BASE_DIR": "/tmp/repos",
        "INDEX_GIT_BIN": "git",
        "GITHUB_API_BASE_URL": "https://api.github.com",
        "GITHUB_TOKEN": "t",
        "GITHUB_WEBHOOK_SECRET": "s",
    }
    cfg = load_config_from_env(environ=environ)
    assert cfg.gitlab is None
    assert cfg.github is not None


def test_load_config_rejects_partial_gitlab() -> None:
    environ = {
        "LLM_BASE_URL": "https://llm.example.com",
        "LLM_API_KEY": "k",
        "LLM_MODEL": "m",
        "INDEX_PG_DSN": "postgresql://user:pass@localhost:5432/db",
        "INDEX_EMBED_MODEL": "text-embedding-3-large",
        "INDEX_EMBED_DIM": "3072",
        "INDEX_REPO_BASE_DIR": "/tmp/repos",
        "INDEX_GIT_BIN": "git",
        "GITLAB_BASE_URL": "https://gitlab.example.com",
        "GITLAB_TOKEN": "t",
    }
    with pytest.raises(ValueError):
        load_config_from_env(environ=environ)


def test_load_config_rejects_partial_github() -> None:
    environ = {
        "LLM_BASE_URL": "https://llm.example.com",
        "LLM_API_KEY": "k",
        "LLM_MODEL": "m",
        "INDEX_PG_DSN": "postgresql://user:pass@localhost:5432/db",
        "INDEX_EMBED_MODEL": "text-embedding-3-large",
        "INDEX_EMBED_DIM": "3072",
        "INDEX_REPO_BASE_DIR": "/tmp/repos",
        "INDEX_GIT_BIN": "git",
        "GITHUB_API_BASE_URL": "https://api.github.com",
        "GITHUB_TOKEN": "t",
    }
    with pytest.raises(ValueError):
        load_config_from_env(environ=environ)



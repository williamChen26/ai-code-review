from __future__ import annotations

import pytest

from app.config import load_config_from_env


def test_load_config_requires_llm() -> None:
    with pytest.raises(ValueError):
        load_config_from_env(environ={})


def test_load_config_requires_at_least_one_scm() -> None:
    environ = {"LLM_BASE_URL": "https://llm.example.com", "LLM_API_KEY": "k", "LLM_MODEL": "m"}
    with pytest.raises(ValueError):
        load_config_from_env(environ=environ)


def test_load_config_gitlab_only_ok() -> None:
    environ = {
        "LLM_BASE_URL": "https://llm.example.com",
        "LLM_API_KEY": "k",
        "LLM_MODEL": "m",
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
        "GITHUB_API_BASE_URL": "https://api.github.com",
        "GITHUB_TOKEN": "t",
    }
    with pytest.raises(ValueError):
        load_config_from_env(environ=environ)



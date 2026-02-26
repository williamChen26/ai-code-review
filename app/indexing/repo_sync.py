from __future__ import annotations

import logging
import os
import subprocess
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)


class RepoSyncer:
    """基于 git CLI 的仓库同步器。"""

    def __init__(self, base_dir: str, git_bin: str) -> None:
        self._base_dir = base_dir
        self._git_bin = git_bin

    def ensure_repo(self, repo_id: str, clone_url: str, target_branch: str, token: str | None, token_user: str | None) -> str:
        repo_dir = _repo_dir(base_dir=self._base_dir, repo_id=repo_id)
        os.makedirs(self._base_dir, exist_ok=True)
        auth_url = _inject_token(clone_url=clone_url, token=token, token_user=token_user)

        if not os.path.exists(repo_dir):
            _run_git(self._git_bin, ["clone", "--branch", target_branch, auth_url, repo_dir], None)
            return repo_dir
        if not os.path.exists(os.path.join(repo_dir, ".git")):
            raise RuntimeError(f"Repo directory exists but is not a git repo: {repo_dir}")

        _run_git(self._git_bin, ["fetch", "--prune", "origin"], repo_dir)
        _run_git(self._git_bin, ["checkout", target_branch], repo_dir)
        _run_git(self._git_bin, ["pull", "origin", target_branch], repo_dir)
        return repo_dir


def _repo_dir(base_dir: str, repo_id: str) -> str:
    safe = repo_id.replace("/", "__").replace(":", "__")
    return os.path.join(base_dir, safe)


def _inject_token(clone_url: str, token: str | None, token_user: str | None) -> str:
    if clone_url.startswith("git@") or clone_url.startswith("ssh://"):
        return clone_url
    if token is None or token_user is None:
        return clone_url
    parsed = urlparse(clone_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid clone_url: {clone_url}")
    netloc = f"{token_user}:{token}@{parsed.netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def _run_git(git_bin: str, args: list[str], cwd: str | None) -> None:
    cmd = [git_bin] + args
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"git failed: {' '.join(cmd)}\nstdout={result.stdout}\nstderr={result.stderr}")
        raise RuntimeError(f"git command failed: {' '.join(cmd)}")

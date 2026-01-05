"""
GitHub API 客户端（外部系统连接器）。

约定：
- 这里只做 HTTP 调用 + 错误处理 + schema 校验
- 出错直接抛错（不要吞），便于定位与告警
"""

from __future__ import annotations

import httpx

from app.github.schemas import GitHubPullRequestFile


class GitHubClient:
    """最小 GitHub API client（支持 list PR files + create PR review）。"""

    def __init__(self, api_base_url: str, token: str, http_client: httpx.AsyncClient) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._token = token
        self._http_client = http_client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def list_pull_request_files(self, owner: str, repo: str, pull_number: int) -> list[GitHubPullRequestFile]:
        """
        拉取 PR 的变更文件列表（包含每个文件的 patch diff）。

        注意：GitHub API 有分页；这里会拉取全部文件。
        """
        per_page = 100
        page = 1
        all_items: list[GitHubPullRequestFile] = []
        while True:
            url = f"{self._api_base_url}/repos/{owner}/{repo}/pulls/{pull_number}/files"
            response = await self._http_client.get(
                url,
                headers=self._headers(),
                params={"per_page": per_page, "page": page},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"GitHub API error {response.status_code}: {response.text}")
            data = response.json()
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected GitHub response shape for PR files: {data}")
            items = [GitHubPullRequestFile.model_validate(x) for x in data]
            all_items.extend(items)
            if len(items) < per_page:
                break
            page += 1
        return all_items

    async def create_pull_request_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        commit_id: str,
        body: str,
    ) -> None:
        """
        创建一条 PR review（会出现在 GitHub 的 “Reviews” 区域）。

        说明：event=COMMENT 表示“评论型 review”（不 approve / request changes）。
        """
        url = f"{self._api_base_url}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
        payload = {"commit_id": commit_id, "body": body, "event": "COMMENT"}
        response = await self._http_client.post(url, headers=self._headers(), json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API error {response.status_code}: {response.text}")



"""
GitLab API 客户端（外部系统连接器）。

约定：
- 这里只做"HTTP 调用 + 错误处理 + schema 校验"，不做业务决策。
- 发生错误时**直接抛错**，不要吞异常（便于定位与告警）。

适用范围：
- GitLab SaaS 和自托管实例均可使用（通过 base_url 区分）
- 鉴权方式：PRIVATE-TOKEN（建议用专用机器人账号的 PAT）
"""

from __future__ import annotations

import logging

import httpx

from app.gitlab.schemas import GitLabMergeRequestChanges
from app.gitlab.schemas import GitLabNote

logger = logging.getLogger(__name__)


class GitLabClient:
    """最小 GitLab API client（v4）。"""

    def __init__(self, base_url: str, private_token: str, http_client: httpx.AsyncClient) -> None:
        """
        - base_url: GitLab 实例地址，例如 https://gitlab.company.com（不含末尾 /）
        - private_token: Personal Access Token（需要 api scope）
        - http_client: 复用的 httpx.AsyncClient
        """
        self._base_url = base_url.rstrip("/")
        self._private_token = private_token
        self._http_client = http_client

    def _headers(self) -> dict[str, str]:
        """GitLab API 鉴权头。"""
        return {"PRIVATE-TOKEN": self._private_token}

    async def get_merge_request_changes(self, project_id: int, mr_iid: int) -> GitLabMergeRequestChanges:
        """
        获取 MR 的文件变更列表（含 diff）。

        API: GET /projects/:id/merge_requests/:iid/changes
        文档: https://docs.gitlab.com/ee/api/merge_requests.html#get-single-merge-request-changes

        参数说明：
        - access_raw_diffs=true：绕过数据库侧的 diff 大小限制，
          直接从 Gitaly 获取原始 diff（对自托管大 MR 尤其重要）。
        """
        url = f"{self._base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
        params = {"access_raw_diffs": "true"}
        response = await self._http_client.get(url, headers=self._headers(), params=params)
        if response.status_code >= 400:
            raise RuntimeError(f"GitLab API error {response.status_code}: {response.text}")

        result = GitLabMergeRequestChanges.model_validate(response.json())

        if result.overflow:
            logger.warning(
                f"GitLab MR !{mr_iid} (project {project_id}) 变更文件过多，"
                f"部分 diff 可能被截断 (overflow=true)"
            )

        return result

    async def post_merge_request_note(self, project_id: int, mr_iid: int, body: str) -> GitLabNote:
        """
        在 MR 下发布一条全局评论（note）。

        API: POST /projects/:id/merge_requests/:iid/notes
        """
        url = f"{self._base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes"
        payload = {"body": body}
        response = await self._http_client.post(url, headers=self._headers(), json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"GitLab API error {response.status_code}: {response.text}")
        return GitLabNote.model_validate(response.json())

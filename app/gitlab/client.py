"""
GitLab API 客户端（外部系统连接器）。

约定：
- 这里只做“HTTP 调用 + 错误处理 + schema 校验”，不做业务决策。
- 发生错误时**直接抛错**，不要吞异常（便于定位与告警）。
"""

from __future__ import annotations

import json

import httpx

from app.gitlab.schemas import GitLabMergeRequestChanges
from app.gitlab.schemas import GitLabNote


class GitLabClient:
    """最小 GitLab API client。后续可加：重试、限流、日志、幂等等。"""

    def __init__(self, base_url: str, private_token: str, http_client: httpx.AsyncClient) -> None:
        """
        - base_url: GitLab 实例地址（不包含末尾 /）
        - private_token: PRIVATE-TOKEN（建议用专用机器人账号）
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
        获取 MR changes（包含每个文件的 diff）。

        说明：
        - GitLab v4 API: GET /projects/:id/merge_requests/:iid/changes
        - 返回用 Pydantic 校验为 `GitLabMergeRequestChanges`
        """
        url = f"{self._base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
        response = await self._http_client.get(url, headers=self._headers())
        if response.status_code >= 400:
            raise RuntimeError(f"GitLab API error {response.status_code}: {response.text}")
        return GitLabMergeRequestChanges.model_validate(response.json())

    async def post_merge_request_note(self, project_id: int, mr_iid: int, body: str) -> GitLabNote:
        """
        在 MR 下发布一条全局评论（note）。

        - 优点：实现简单，适合 Day 1 跑通闭环
        - 后续：可以扩展为行内评论（discussions + position）
        """
        url = f"{self._base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes"
        payload = {"body": body}
        response = await self._http_client.post(url, headers=self._headers(), json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"GitLab API error {response.status_code}: {response.text}")
        return GitLabNote.model_validate(response.json())

    async def post_inline_comment_placeholder(
        self,
        project_id: int,
        mr_iid: int,
        body: str,
        position: dict[str, object],
    ) -> GitLabNote:
        """
        预留：GitLab 行内评论需要 /discussions + position 结构（diff_refs + new_path/new_line...）。
        这里先把 position 编码进 body，避免“实现了但不可用”的半成品接口。
        """
        debug = json.dumps(position, ensure_ascii=False, sort_keys=True)
        return await self.post_merge_request_note(
            project_id=project_id,
            mr_iid=mr_iid,
            body=f"{body}\n\n[position placeholder]\n```json\n{debug}\n```",
        )



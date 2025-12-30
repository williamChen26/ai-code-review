"""
本地 Mock GitLab API server（只覆盖最小闭环用到的两个接口）。

用途：
- 在没有真实 GitLab 的情况下，本地跑通：
  Webhook -> get MR changes -> post MR note

启动：
  python -m app.dev.mock_gitlab_server
"""

from __future__ import annotations

import time

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel


class NoteCreateRequest(BaseModel):
    body: str


def _default_changes_response() -> dict[str, object]:
    return {
        "changes": [
            {
                "old_path": "src/example.py",
                "new_path": "src/example.py",
                "a_mode": "100644",
                "b_mode": "100644",
                "new_file": False,
                "renamed_file": False,
                "deleted_file": False,
                "diff": (
                    "@@ -1,3 +1,6 @@\n"
                    " def add(a: int, b: int) -> int:\n"
                    "-    return a + b\n"
                    "+    # TODO: handle None inputs\n"
                    "+    return a + b\n"
                    "+\n"
                    "+def sub(a: int, b: int) -> int:\n"
                    "+    return a - b\n"
                ),
            }
        ],
        "diff_refs": {
            "base_sha": "0000000000000000000000000000000000000000",
            "head_sha": "1111111111111111111111111111111111111111",
            "start_sha": "0000000000000000000000000000000000000000",
        },
    }


app = FastAPI(title="Mock GitLab API", version="0.1.0")

_notes: list[dict[str, object]] = []


@app.get("/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes")
async def get_merge_request_changes(project_id: int, mr_iid: int) -> dict[str, object]:
    _ = project_id
    _ = mr_iid
    return _default_changes_response()


@app.post("/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes")
async def post_merge_request_note(project_id: int, mr_iid: int, req: NoteCreateRequest) -> dict[str, object]:
    note_id = len(_notes) + 1
    note = {
        "id": note_id,
        "body": req.body,
        "project_id": project_id,
        "mr_iid": mr_iid,
        "created_at": int(time.time()),
    }
    _notes.append(note)
    return {"id": note_id, "body": req.body}


@app.get("/__debug__/notes")
async def debug_notes() -> dict[str, object]:
    return {"count": len(_notes), "notes": _notes}


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=9002)


if __name__ == "__main__":
    main()



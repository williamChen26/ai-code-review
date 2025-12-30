"""
本地 Mock OpenAI-compatible LLM server。

用途：
- 在没有真实 LLM 网关的情况下，本地跑通闭环（planner/reviewer 的 JSON-only 输出）

启动：
  python -m app.dev.mock_openai_server
"""

from __future__ import annotations

from collections.abc import Sequence

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.llm.client import ChatMessage


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(default_factory=list)


def _extract_changed_paths_from_planner_prompt(prompt: str) -> list[str]:
    """
    从 planner prompt 里提取文件 path 列表。

    形如：
      变更文件：
      - a/b.py (python)
      - README.md (markdown)
    """
    lines = prompt.splitlines()
    paths: list[str] = []
    in_files_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("变更文件："):
            in_files_section = True
            continue
        if not in_files_section:
            continue
        if not stripped.startswith("- "):
            continue
        raw = stripped.removeprefix("- ").strip()
        if " (" in raw:
            path = raw.split(" (", 1)[0].strip()
        else:
            path = raw.strip()
        if path:
            paths.append(path)
    return paths


def _extract_path_from_reviewer_prompt(prompt: str) -> str:
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("path: "):
            return stripped.removeprefix("path: ").strip()
    raise ValueError("Cannot find `path: ...` in reviewer prompt")


def _build_mock_risk_plan_json(changed_paths: list[str]) -> str:
    high_risk = changed_paths[:1]
    return (
        "{"
        f"\"highRiskFiles\": {high_risk!r}, "
        "\"reviewFocus\": [\"correctness\", \"security\"], "
        "\"reviewDepth\": \"normal\""
        "}"
    ).replace("'", '"')


def _build_mock_file_review_json(path: str) -> str:
    return (
        "{"
        "\"comments\": ["
        "{"
        f"\"path\": {path!r}, "
        "\"severity\": \"warning\", "
        "\"message\": \"[MOCK] 建议补充更严格的错误处理与边界校验，并为关键逻辑添加单元测试。\""
        "}"
        "]"
        "}"
    ).replace("'", '"')


def _decide_mock_response(messages: Sequence[ChatMessage]) -> str:
    user_texts = [m.content for m in messages if m.role == "user"]
    if not user_texts:
        raise ValueError("Mock server expects at least one user message")
    prompt = "\n".join(user_texts)

    if "\"highRiskFiles\"" in prompt and "变更文件" in prompt:
        changed_paths = _extract_changed_paths_from_planner_prompt(prompt=prompt)
        return _build_mock_risk_plan_json(changed_paths=changed_paths)

    if "\"comments\"" in prompt and "diff:" in prompt:
        path = _extract_path_from_reviewer_prompt(prompt=prompt)
        return _build_mock_file_review_json(path=path)

    # 兜底：返回一个“空评论”的 reviewer 结构，避免流程卡死
    return "{\"comments\": []}"


app = FastAPI(title="Mock OpenAI-compatible LLM", version="0.1.0")


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest) -> dict[str, object]:
    content = _decide_mock_response(messages=req.messages)
    return {"choices": [{"message": {"content": content}}]}


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=9001)


if __name__ == "__main__":
    main()



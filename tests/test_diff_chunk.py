from __future__ import annotations

import pytest

from app.agent.schemas import GetDiffChunkArgs
from app.agent.schemas import ToolContext
from app.agent.tools.diff import get_diff_chunk


def test_get_diff_chunk_limits_lines() -> None:
    ctx = ToolContext(diff_by_path={"a.py": "line1\nline2\nline3\n"})
    args = GetDiffChunkArgs(path="a.py", max_lines=2)
    chunk = get_diff_chunk(args=args, ctx=ctx)
    assert chunk == "line1\nline2"


def test_get_diff_chunk_missing_path_raises() -> None:
    ctx = ToolContext(diff_by_path={})
    args = GetDiffChunkArgs(path="missing.py", max_lines=1)
    with pytest.raises(KeyError):
        get_diff_chunk(args=args, ctx=ctx)



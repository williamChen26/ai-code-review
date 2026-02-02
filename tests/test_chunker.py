from __future__ import annotations

from app.indexing.chunker import chunk_file


def test_chunk_file_includes_imports_and_function() -> None:
    content = "\n".join(
        [
            "import os",
            "from typing import Any",
            "",
            "def add(a: int, b: int) -> int:",
            "    return a + b",
        ]
    )
    chunks = chunk_file(repo_id="github:owner/repo", path="src/app.py", content=content)
    kinds = {c.symbol_type for c in chunks}
    assert "module_imports" in kinds
    assert "function_definition" in kinds

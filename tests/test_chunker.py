from __future__ import annotations

from app.indexing.parser import parse_file


def test_parse_file_extracts_imports_and_function() -> None:
    content = "\n".join(
        [
            "import os",
            "from typing import Any",
            "",
            "def add(a: int, b: int) -> int:",
            "    return a + b",
        ]
    )
    parsed = parse_file(path="src/app.py", content=content, language="python")
    assert len(parsed.imports) >= 2
    assert any(s.name == "add" and s.kind == "function" for s in parsed.symbols)
    assert parsed.summary_material  # should contain file summary


def test_parse_file_extracts_calls() -> None:
    content = "\n".join(
        [
            "def greet(name: str) -> str:",
            "    result = format_name(name)",
            "    print(result)",
            "    return result",
        ]
    )
    parsed = parse_file(path="src/greet.py", content=content, language="python")
    assert len(parsed.symbols) == 1
    sym = parsed.symbols[0]
    assert "format_name" in sym.calls
    assert "print" in sym.calls

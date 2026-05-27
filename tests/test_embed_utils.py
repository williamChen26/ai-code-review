"""
测试 embed_utils 的文本构建和截断逻辑。

可本地运行：
    pytest tests/test_embed_utils.py -v
"""

from __future__ import annotations

from app.indexing.embed_utils import (
    EMBED_MAX_CHARS,
    _TRUNCATION_MARKER,
    build_embedding_text,
    truncate_code,
)


# ---------------------------------------------------------------------------
# truncate_code
# ---------------------------------------------------------------------------

def test_truncate_code_short_unchanged() -> None:
    """短于阈值的 code 不做任何修改。"""
    code = "def foo():\n    return 1\n"
    assert truncate_code(code) == code


def test_truncate_code_exact_limit_unchanged() -> None:
    """恰好等于阈值的 code 不截断。"""
    code = "x" * EMBED_MAX_CHARS
    assert truncate_code(code) == code


def test_truncate_code_over_limit_truncated() -> None:
    """超过阈值的 code 截断到阈值并追加标记。"""
    code = "x" * (EMBED_MAX_CHARS + 500)
    result = truncate_code(code)
    assert result.endswith(_TRUNCATION_MARKER)
    assert len(result) == EMBED_MAX_CHARS + len(_TRUNCATION_MARKER)


def test_truncate_code_preserves_head() -> None:
    """截断保留代码头部。"""
    head = "def important_function(a: int, b: str):\n"
    code = head + "x" * (EMBED_MAX_CHARS + 1000)
    result = truncate_code(code)
    assert result.startswith(head)


# ---------------------------------------------------------------------------
# build_embedding_text
# ---------------------------------------------------------------------------

def test_build_embedding_text_format() -> None:
    """输出格式正确。"""
    result = build_embedding_text(path="src/utils.py", code="def hello():\n    pass")
    assert result == "File: src/utils.py\n\nCode:\ndef hello():\n    pass"


def test_build_embedding_text_truncates_long_code() -> None:
    """超长 code 被截断。"""
    long_code = "a" * (EMBED_MAX_CHARS + 100)
    result = build_embedding_text(path="big.py", code=long_code)
    assert _TRUNCATION_MARKER in result
    code_part = result.split("Code:\n", maxsplit=1)[1]
    assert len(code_part) == EMBED_MAX_CHARS + len(_TRUNCATION_MARKER)


def test_build_embedding_text_short_code_no_marker() -> None:
    """短 code 不含截断标记。"""
    result = build_embedding_text(path="a.py", code="x = 1")
    assert _TRUNCATION_MARKER not in result


def test_index_and_query_consistency() -> None:
    """索引侧和查询侧用同一函数，输出必须一致。"""
    path = "src/components/App.tsx"
    code = "export const App = () => {\n  return <div>Hello</div>;\n};"
    index_text = build_embedding_text(path=path, code=code)
    query_text = build_embedding_text(path=path, code=code)
    assert index_text == query_text


def test_index_and_query_consistency_with_truncation() -> None:
    """超长 code 时，索引侧和查询侧的截断结果也必须一致。"""
    path = "big_file.py"
    code = "def big():\n" + "    x = 1\n" * 10000
    index_text = build_embedding_text(path=path, code=code)
    query_text = build_embedding_text(path=path, code=code)
    assert index_text == query_text
    assert _TRUNCATION_MARKER in index_text

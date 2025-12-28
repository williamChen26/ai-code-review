from __future__ import annotations

import pytest

from app.agent.schemas import CalcPythonComplexityArgs
from app.agent.schemas import ToolContext
from app.agent.tools.complexity import calc_python_complexity


def test_calc_python_complexity_empty_diff_is_zero() -> None:
    ctx = ToolContext(diff_by_path={"a.py": ""})
    args = CalcPythonComplexityArgs(path="a.py")
    result = calc_python_complexity(args=args, ctx=ctx)
    assert result["complexity"] == 0


def test_calc_python_complexity_counts_branches() -> None:
    diff = "\n".join(
        [
            "+++ b/a.py",
            "@@",
            "+def f(x):",
            "+    if x:",
            "+        return 1",
            "+    for i in range(3):",
            "+        pass",
        ]
    )
    ctx = ToolContext(diff_by_path={"a.py": diff})
    args = CalcPythonComplexityArgs(path="a.py")
    result = calc_python_complexity(args=args, ctx=ctx)
    assert result["complexity"] >= 3


def test_calc_python_complexity_invalid_python_raises() -> None:
    diff = "\n".join(["+++ b/a.py", "+def f(:", "+    pass"])
    ctx = ToolContext(diff_by_path={"a.py": diff})
    args = CalcPythonComplexityArgs(path="a.py")
    with pytest.raises(SyntaxError):
        calc_python_complexity(args=args, ctx=ctx)



from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class ToolContext(BaseModel):
    diff_by_path: dict[str, str]


class GetDiffChunkArgs(BaseModel):
    path: str
    max_lines: int


class FindRiskyPatternArgs(BaseModel):
    path: str
    patterns: list[str]


class CalcPythonComplexityArgs(BaseModel):
    path: str


class GetDiffChunkCall(BaseModel):
    name: Literal["get_diff_chunk"]
    args: GetDiffChunkArgs


class FindRiskyPatternCall(BaseModel):
    name: Literal["find_risky_pattern"]
    args: FindRiskyPatternArgs


class CalcPythonComplexityCall(BaseModel):
    name: Literal["calc_python_complexity"]
    args: CalcPythonComplexityArgs


ToolCall = Annotated[
    Union[GetDiffChunkCall, FindRiskyPatternCall, CalcPythonComplexityCall],
    Field(discriminator="name"),
]


class AgentAction(BaseModel):
    kind: Literal["action"]
    call: ToolCall


class AgentFinal(BaseModel):
    kind: Literal["final"]
    answer: str


AgentStep = Annotated[Union[AgentAction, AgentFinal], Field(discriminator="kind")]



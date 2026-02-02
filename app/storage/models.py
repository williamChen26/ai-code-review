from __future__ import annotations

from pydantic import BaseModel


class FileIndexEntry(BaseModel):
    repo_id: str
    path: str
    language: str
    checksum: str


class CodeChunk(BaseModel):
    repo_id: str
    path: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    content: str
    checksum: str

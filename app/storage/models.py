"""
存储层数据模型（Pydantic）。

三表分层设计：
- FileRecord:      文件级信息（语言、校验和、摘要材料）
- SymbolRecord:    symbol 级信息（函数/类/方法，含代码、imports、calls）
- EmbeddingRecord: 向量表，与业务表解耦，通过 target_type + target_key 关联
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FileRecord(BaseModel):
    """文件级索引记录。

    - checksum: 文件内容的 SHA256，用于增量判断是否需要重建
    - summary_material: imports + symbol signatures 拼接文本，用于 file 级 embedding
    """

    repo_id: str
    path: str
    language: str
    checksum: str
    summary_material: str = ""


class SymbolRecord(BaseModel):
    """Symbol 级索引记录（函数/类/方法）。

    - kind: "function" | "class" | "method" 等
    - code: symbol 的完整源代码
    - imports: 该文件中的 import 名称列表（用于理解依赖上下文）
    - calls: 该 symbol 内调用的函数名列表（用于构建调用关系图）
    """

    repo_id: str
    path: str
    name: str
    kind: str
    start_line: int
    end_line: int
    code: str
    checksum: str
    imports: list[str] = Field(default_factory=list)
    calls: list[str] = Field(default_factory=list)


class EmbeddingRecord(BaseModel):
    """向量记录，与 files/symbols 业务表解耦。

    - target_type: "symbol" 或 "file"
    - target_key: 唯一标识
        - symbol 类型: "path::name::start_line"
        - file 类型: "path"
    - embedding: float 向量
    """

    repo_id: str
    target_type: str
    target_key: str
    embedding: list[float] = Field(default_factory=list)


def build_symbol_target_key(path: str, name: str, start_line: int) -> str:
    """构建 symbol 类型的 embedding target_key。"""
    return f"{path}::{name}::{start_line}"


def build_file_target_key(path: str) -> str:
    """构建 file 类型的 embedding target_key。"""
    return path
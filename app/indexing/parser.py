"""
AST 结构化解析器。

职责：
- 按文件做 tree-sitter 解析
- 提取 symbol（函数/类/方法）
- 提取 imports / exports
- 提取调用关系（函数调用名称）
- 生成 file summary material（imports + symbol signatures）

支持语言：Python、TypeScript、JavaScript（MVP）

设计：
- 纯函数，无状态
- 解析失败不静默，但会降级处理（无法解析的语言返回空结果）
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 解析结果模型（纯数据，不依赖 Pydantic / 存储层）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SymbolInfo:
    """从 AST 提取的单个 symbol 信息。"""

    name: str
    kind: str           # "function" | "class" | "method"
    start_line: int     # 1-based
    end_line: int       # 1-based
    code: str           # 完整源代码文本
    signature: str      # 函数签名（用于 summary material）
    calls: list[str]    # 该 symbol 内调用的函数名列表


@dataclass(frozen=True)
class ParsedFile:
    """单个文件的完整解析结果。"""

    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)      # 导入的模块/符号名
    exports: list[str] = field(default_factory=list)      # 导出的符号名（TS/JS）
    summary_material: str = ""                             # imports + signatures 拼接文本


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def parse_file(path: str, content: str, language: str) -> ParsedFile:
    """解析单个文件，返回结构化的 ParsedFile。

    - 如果 language 不支持或 tree-sitter 解析失败，返回空的 ParsedFile
    - 不抛异常（调用方可安全忽略不支持的文件）
    """
    parser = _try_get_parser(language=language)
    if parser is None:
        logger.debug(f"No parser available for language={language}, path={path}")
        return ParsedFile()

    try:
        tree = parser.parse(content.encode("utf-8"))
    except Exception as exc:
        logger.warning(f"tree-sitter parse failed for {path}: {exc}")
        return ParsedFile()

    root = tree.root_node
    lines = content.splitlines()

    # 1. 提取 imports
    imports = _extract_imports(language=language, root=root)

    # 2. 提取 exports（仅 TS/JS）
    exports = _extract_exports(language=language, root=root)

    # 3. 提取 symbols + 每个 symbol 的 calls
    symbols = _extract_symbols(language=language, root=root, lines=lines)

    # 4. 构建 summary material
    summary_material = _build_summary_material(
        path=path, imports=imports, symbols=symbols,
    )

    logger.debug(
        f"Parsed {path}: {len(symbols)} symbols, "
        f"{len(imports)} imports, {len(exports)} exports"
    )
    return ParsedFile(
        symbols=symbols,
        imports=imports,
        exports=exports,
        summary_material=summary_material,
    )


def compute_checksum(text: str) -> str:
    """计算文本的 SHA256 校验和。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# tree-sitter 工具
# ---------------------------------------------------------------------------

def _try_get_parser(language: str):  # type: ignore[return]
    """尝试获取 tree-sitter parser，失败返回 None。"""
    if language not in _SUPPORTED_LANGUAGES:
        return None
    try:
        return get_parser(language)
    except Exception:
        return None


_SUPPORTED_LANGUAGES = {"python", "typescript", "javascript"}


# ---------------------------------------------------------------------------
# Symbol 提取
# ---------------------------------------------------------------------------

# 每种语言对应的 AST node type -> symbol kind 映射
_SYMBOL_NODE_TYPES: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "arrow_function": "function",
    },
    "javascript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "arrow_function": "function",
    },
}


def _extract_symbols(
    language: str, root: Node, lines: list[str],
) -> list[SymbolInfo]:
    """从 AST 中提取所有 symbol 节点。"""
    type_map = _SYMBOL_NODE_TYPES.get(language, {})
    if not type_map:
        return []

    results: list[SymbolInfo] = []
    stack: list[tuple[Node, bool]] = [(root, False)]  # (node, is_inside_class)

    while stack:
        node, inside_class = stack.pop()

        if node.type in type_map:
            kind = type_map[node.type]
            # Python: 类内部的 function_definition 视为 method
            if language == "python" and kind == "function" and inside_class:
                kind = "method"

            name = _node_symbol_name(node=node)
            # arrow_function 特殊处理：取父节点的变量名
            if node.type == "arrow_function":
                name = _resolve_arrow_function_name(node=node)
                if not name:
                    # 匿名箭头函数，跳过
                    for child in reversed(node.children):
                        stack.append((child, inside_class))
                    continue

            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            if start_line < 1 or end_line < start_line:
                continue

            code = "\n".join(lines[start_line - 1: end_line])
            signature = _extract_signature(
                language=language, node=node, name=name, lines=lines,
            )
            calls = _extract_calls_from_node(
                language=language, node=node,
            )

            results.append(SymbolInfo(
                name=name,
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                code=code,
                signature=signature,
                calls=calls,
            ))

            # 如果是 class，其子节点的 function 应该标记为 method
            if kind == "class":
                for child in reversed(node.children):
                    stack.append((child, True))
                continue

        # 继续遍历子节点
        for child in reversed(node.children):
            stack.append((child, inside_class))

    # 按 start_line 排序
    results.sort(key=lambda s: s.start_line)
    return results


def _node_symbol_name(node: Node) -> str:
    """获取 AST node 的名称。"""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return ""
    return name_node.text.decode("utf-8")


def _resolve_arrow_function_name(node: Node) -> str:
    """尝试从 arrow function 的父节点获取变量名。

    处理模式：
    - const foo = () => {}     -> variable_declarator -> name
    - export const foo = ...   -> 同上（再上一层）
    """
    parent = node.parent
    if parent is None:
        return ""
    # variable_declarator: const foo = () => {}
    if parent.type == "variable_declarator":
        name_node = parent.child_by_field_name("name")
        if name_node is not None:
            return name_node.text.decode("utf-8")
    # pair in object: { foo: () => {} }
    if parent.type == "pair":
        key_node = parent.child_by_field_name("key")
        if key_node is not None:
            return key_node.text.decode("utf-8")
    return ""


def _extract_signature(
    language: str, node: Node, name: str, lines: list[str],
) -> str:
    """提取 symbol 的签名（第一行或到冒号/大括号的部分）。

    用于 summary material，不需要完整代码。
    """
    start_line = node.start_point[0]
    if start_line >= len(lines):
        return name

    first_line = lines[start_line].strip()

    if language == "python":
        # Python: 取到冒号为止
        # def foo(a: int, b: str) -> bool:
        if ":" in first_line:
            return first_line.split(":")[0].strip() + ":"
        return first_line

    # TS/JS: 取到左大括号为止
    if "{" in first_line:
        return first_line.split("{")[0].strip() + " {"
    return first_line


# ---------------------------------------------------------------------------
# Import 提取
# ---------------------------------------------------------------------------

def _extract_imports(language: str, root: Node) -> list[str]:
    """提取文件中的所有 import 名称。"""
    if language == "python":
        return _extract_python_imports(root=root)
    if language in {"typescript", "javascript"}:
        return _extract_ts_js_imports(root=root)
    return []


def _extract_python_imports(root: Node) -> list[str]:
    """Python import 提取。

    处理：
    - import os            -> ["os"]
    - from os.path import join -> ["os.path.join"]
    - import os, sys       -> ["os", "sys"]
    - from typing import List, Dict -> ["typing.List", "typing.Dict"]
    """
    imports: list[str] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type == "import_statement":
            # import os / import os, sys
            for child in node.children:
                if child.type == "dotted_name":
                    imports.append(child.text.decode("utf-8"))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node is not None:
                        imports.append(name_node.text.decode("utf-8"))
        elif node.type == "import_from_statement":
            # from X import Y, Z
            module_name = ""
            imported_names: list[str] = []
            for child in node.children:
                if child.type == "dotted_name":
                    if not module_name:
                        module_name = child.text.decode("utf-8")
                    else:
                        imported_names.append(child.text.decode("utf-8"))
                elif child.type == "import_list" or child.type == "aliased_import":
                    for sub in child.children if child.type == "import_list" else [child]:
                        if sub.type == "dotted_name" or sub.type == "identifier":
                            imported_names.append(sub.text.decode("utf-8"))
                        elif sub.type == "aliased_import":
                            name_node = sub.child_by_field_name("name")
                            if name_node is not None:
                                imported_names.append(name_node.text.decode("utf-8"))
            for name in imported_names:
                imports.append(f"{module_name}.{name}" if module_name else name)
            if not imported_names and module_name:
                imports.append(module_name)
        else:
            stack.extend(reversed(node.children))
    return imports


def _extract_ts_js_imports(root: Node) -> list[str]:
    """TypeScript/JavaScript import 提取。

    处理：
    - import foo from 'bar'        -> ["bar.foo"]
    - import { a, b } from 'bar'   -> ["bar.a", "bar.b"]
    - import * as ns from 'bar'    -> ["bar"]
    """
    imports: list[str] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type == "import_statement":
            source = _find_child_by_type(node=node, target_type="string")
            module_name = source.text.decode("utf-8").strip("'\"") if source else ""

            # 查找 import_clause 下的 named_imports
            for child in node.children:
                if child.type == "import_clause":
                    for sub in child.children:
                        if sub.type == "identifier":
                            # default import
                            name = sub.text.decode("utf-8")
                            imports.append(f"{module_name}.{name}" if module_name else name)
                        elif sub.type == "named_imports":
                            for spec in sub.children:
                                if spec.type == "import_specifier":
                                    name_node = spec.child_by_field_name("name")
                                    if name_node is not None:
                                        name = name_node.text.decode("utf-8")
                                        imports.append(
                                            f"{module_name}.{name}" if module_name else name
                                        )
                        elif sub.type == "namespace_import":
                            imports.append(module_name)
            if not any(c.type == "import_clause" for c in node.children) and module_name:
                imports.append(module_name)
        else:
            stack.extend(reversed(node.children))
    return imports


# ---------------------------------------------------------------------------
# Export 提取（仅 TS/JS）
# ---------------------------------------------------------------------------

def _extract_exports(language: str, root: Node) -> list[str]:
    """提取导出的符号名称（仅 TS/JS）。"""
    if language not in {"typescript", "javascript"}:
        return []
    exports: list[str] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type == "export_statement":
            # export function foo / export class Bar / export const baz
            for child in node.children:
                name = _node_symbol_name(node=child)
                if name:
                    exports.append(name)
                # export { a, b }
                if child.type == "export_clause":
                    for spec in child.children:
                        if spec.type == "export_specifier":
                            name_node = spec.child_by_field_name("name")
                            if name_node is not None:
                                exports.append(name_node.text.decode("utf-8"))
        else:
            stack.extend(reversed(node.children))
    return exports


# ---------------------------------------------------------------------------
# 调用关系提取
# ---------------------------------------------------------------------------

def _extract_calls_from_node(language: str, node: Node) -> list[str]:
    """提取某个 symbol 节点内的所有函数调用名称。

    MVP：只提取直接函数调用名，不做跨文件解析。
    例如: foo() -> "foo", self.bar() -> "bar", obj.method() -> "method"
    """
    calls: set[str] = set()
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        if current.type == "call" and language == "python":
            # Python: call -> function (identifier / attribute)
            func_node = current.child_by_field_name("function")
            if func_node is not None:
                name = _resolve_call_name(func_node=func_node)
                if name:
                    calls.add(name)
        elif current.type == "call_expression" and language in {"typescript", "javascript"}:
            # TS/JS: call_expression -> function (identifier / member_expression)
            func_node = current.child_by_field_name("function")
            if func_node is not None:
                name = _resolve_call_name(func_node=func_node)
                if name:
                    calls.add(name)
        stack.extend(reversed(current.children))
    return sorted(calls)


def _resolve_call_name(func_node: Node) -> str:
    """从调用表达式的 function 子节点解析出函数名。

    - identifier: foo() -> "foo"
    - attribute / member_expression: self.bar() / obj.method() -> "bar" / "method"
    """
    if func_node.type == "identifier":
        return func_node.text.decode("utf-8")
    if func_node.type in {"attribute", "member_expression"}:
        # 取最后一个 property/attribute
        prop = func_node.child_by_field_name("attribute") or func_node.child_by_field_name("property")
        if prop is not None:
            return prop.text.decode("utf-8")
    return ""


# ---------------------------------------------------------------------------
# Summary Material
# ---------------------------------------------------------------------------

def _build_summary_material(
    path: str,
    imports: list[str],
    symbols: list[SymbolInfo],
) -> str:
    """构建文件级 summary material（用于 file embedding）。

    格式：
    File: path
    Imports: a, b, c
    Symbols:
    - function foo(a, b):
    - class Bar:
    """
    parts: list[str] = [f"File: {path}"]
    if imports:
        parts.append(f"Imports: {', '.join(imports)}")
    if symbols:
        parts.append("Symbols:")
        for sym in symbols:
            parts.append(f"- {sym.kind} {sym.signature}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _find_child_by_type(node: Node, target_type: str) -> Node | None:
    """在 node 的直接子节点中查找指定类型的节点。"""
    for child in node.children:
        if child.type == target_type:
            return child
    return None

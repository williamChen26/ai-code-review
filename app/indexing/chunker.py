from __future__ import annotations

import hashlib
from collections.abc import Iterable

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from app.review.context import infer_language_from_path
from app.storage.models import CodeChunk


def chunk_file(repo_id: str, path: str, content: str) -> list[CodeChunk]:
    language = infer_language_from_path(path=path)
    lines = content.splitlines()
    chunks: list[CodeChunk] = []

    import_chunk = _build_import_chunk(repo_id=repo_id, path=path, lines=lines)
    if import_chunk is not None:
        chunks.append(import_chunk)

    parser = _try_get_parser(language=language)
    if parser is None:
        chunks.append(_build_file_chunk(repo_id=repo_id, path=path, content=content))
        return chunks

    tree = parser.parse(content.encode("utf-8"))
    nodes = _collect_symbol_nodes(language=language, root=tree.root_node)
    for node in nodes:
        chunk = _node_to_chunk(repo_id=repo_id, path=path, node=node, lines=lines)
        if chunk is not None:
            chunks.append(chunk)

    if not nodes:
        chunks.append(_build_file_chunk(repo_id=repo_id, path=path, content=content))
    return chunks


def _try_get_parser(language: str):
    try:
        return get_parser(language)
    except Exception:
        return None


def _collect_symbol_nodes(language: str, root: Node) -> list[Node]:
    targets = _node_types_for_language(language=language)
    found: list[Node] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type in targets:
            found.append(node)
        stack.extend(reversed(node.children))
    return found


def _node_types_for_language(language: str) -> set[str]:
    if language == "python":
        return {"function_definition", "class_definition"}
    if language in {"javascript", "typescript"}:
        return {"function_declaration", "class_declaration", "method_definition"}
    if language == "go":
        return {"function_declaration", "method_declaration", "type_spec"}
    if language == "java":
        return {"method_declaration", "class_declaration"}
    return set()


def _node_to_chunk(repo_id: str, path: str, node: Node, lines: list[str]) -> CodeChunk | None:
    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1
    if start_line < 1 or end_line < start_line:
        return None
    content = "\n".join(lines[start_line - 1 : end_line])
    name = _node_symbol_name(node=node)
    checksum = _sha256(content)
    return CodeChunk(
        repo_id=repo_id,
        path=path,
        symbol_name=name,
        symbol_type=node.type,
        start_line=start_line,
        end_line=end_line,
        content=content,
        checksum=checksum,
    )


def _node_symbol_name(node: Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return "anonymous"
    return name_node.text.decode("utf-8")


def _build_file_chunk(repo_id: str, path: str, content: str) -> CodeChunk:
    checksum = _sha256(content)
    line_count = len(content.splitlines())
    return CodeChunk(
        repo_id=repo_id,
        path=path,
        symbol_name="__file__",
        symbol_type="file",
        start_line=1,
        end_line=line_count if line_count > 0 else 1,
        content=content,
        checksum=checksum,
    )


def _build_import_chunk(repo_id: str, path: str, lines: list[str]) -> CodeChunk | None:
    import_lines: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            import_lines.append((idx, line))
        if stripped.startswith("export "):
            import_lines.append((idx, line))
    if not import_lines:
        return None
    start_line = import_lines[0][0]
    end_line = import_lines[-1][0]
    content = "\n".join([l for _, l in import_lines])
    return CodeChunk(
        repo_id=repo_id,
        path=path,
        symbol_name="__imports__",
        symbol_type="module_imports",
        start_line=start_line,
        end_line=end_line,
        content=content,
        checksum=_sha256(content),
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

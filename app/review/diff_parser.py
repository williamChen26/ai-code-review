from __future__ import annotations


def extract_changed_line_numbers(diff: str) -> list[int]:
    lines = diff.splitlines()
    changed: list[int] = []
    new_line = 0
    old_line = 0
    for line in lines:
        if line.startswith("@@"):
            old_line, new_line = _parse_hunk_header(header=line)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            changed.append(new_line)
            new_line += 1
            continue
        if line.startswith("-") and not line.startswith("---"):
            old_line += 1
            continue
        if line.startswith(" "):
            old_line += 1
            new_line += 1
            continue
    return changed


def _parse_hunk_header(header: str) -> tuple[int, int]:
    # @@ -a,b +c,d @@
    try:
        parts = header.split(" ")
        old_part = parts[1]
        new_part = parts[2]
        old_start = int(old_part.split(",")[0].lstrip("-"))
        new_start = int(new_part.split(",")[0].lstrip("+"))
        return old_start, new_start
    except Exception as exc:
        raise ValueError(f"Invalid diff hunk header: {header}") from exc

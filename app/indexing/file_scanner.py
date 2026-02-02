from __future__ import annotations

import os


def scan_repo_files(repo_dir: str, allowed_extensions: set[str], max_bytes: int) -> list[str]:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be > 0")
    files: list[str] = []
    for root, dirs, filenames in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".venv", "dist", "build", "__pycache__"}]
        for name in filenames:
            path = os.path.join(root, name)
            ext = os.path.splitext(name)[1].lower()
            if ext not in allowed_extensions:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > max_bytes:
                continue
            files.append(path)
    return files

"""
索引 dry-run 调试脚本：扫描 + AST 解析 + 模拟分 chunk，不连 DB 不调 API。

用途：
- 验证文件扫描和过滤逻辑
- 验证 AST 解析对目标项目是否正常
- 观察 chunk 分组和预估 embedding 调用量
- 排查解析失败的文件

用法：
    python scripts/debug_index_dryrun.py /path/to/your/frontend/project

    # 指定 chunk_size
    python scripts/debug_index_dryrun.py /path/to/project --chunk-size 30

    # 只扫描不解析（快速看文件列表）
    python scripts/debug_index_dryrun.py /path/to/project --scan-only
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.indexing.file_scanner import scan_repo_files
from app.indexing.indexer import ALLOWED_EXTENSIONS, CHUNK_SIZE, MAX_FILE_BYTES, _split_chunks
from app.indexing.parser import parse_file
from app.review.context import infer_language_from_path


def main() -> None:
    parser = argparse.ArgumentParser(description="索引 dry-run：扫描 + 解析，不连 DB 不调 API")
    parser.add_argument("repo_dir", help="目标仓库本地路径")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE, help=f"chunk 大小 (默认 {CHUNK_SIZE})")
    parser.add_argument("--scan-only", action="store_true", help="只扫描文件列表，不做 AST 解析")
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)
    if not os.path.isdir(repo_dir):
        print(f"错误: 目录不存在: {repo_dir}")
        sys.exit(1)

    print(f"=" * 60)
    print(f"索引 Dry-Run")
    print(f"目录: {repo_dir}")
    print(f"允许扩展名: {sorted(ALLOWED_EXTENSIONS)}")
    print(f"最大文件大小: {MAX_FILE_BYTES / 1024:.0f} KB")
    print(f"Chunk 大小: {args.chunk_size}")
    print(f"=" * 60)

    # Step 1: 扫描
    t0 = time.monotonic()
    files = scan_repo_files(
        repo_dir=repo_dir,
        allowed_extensions=ALLOWED_EXTENSIONS,
        max_bytes=MAX_FILE_BYTES,
    )
    relative_paths = [os.path.relpath(path, repo_dir) for path in files]
    scan_time = time.monotonic() - t0

    print(f"\n📂 扫描完成: {len(relative_paths)} 个文件 ({scan_time:.2f}s)")

    # 按扩展名统计
    ext_counts: dict[str, int] = {}
    for p in relative_paths:
        ext = os.path.splitext(p)[1].lower()
        ext_counts[ext] = ext_counts.get(ext, 0) + 1

    print("\n按扩展名分布:")
    for ext, count in sorted(ext_counts.items(), key=lambda x: -x[1]):
        print(f"  {ext:8s} → {count} 个文件")

    # chunk 分组预览
    chunks = _split_chunks(relative_paths, args.chunk_size)
    print(f"\n分 chunk: {len(chunks)} 组 (每组最多 {args.chunk_size} 个文件)")

    if args.scan_only:
        print("\n[--scan-only] 跳过 AST 解析")
        _print_file_list(relative_paths)
        return

    # Step 2: AST 解析
    print(f"\n🔧 开始 AST 解析...")
    from app.indexing.embed_utils import EMBED_MAX_CHARS

    total_symbols = 0
    total_imports = 0
    parse_errors: list[str] = []
    file_details: list[tuple[str, int, int]] = []
    oversized_symbols: list[tuple[str, str, int]] = []

    for i, chunk in enumerate(chunks):
        chunk_t0 = time.monotonic()
        chunk_symbols = 0
        for path in chunk:
            full_path = os.path.join(repo_dir, path)
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError as e:
                parse_errors.append(f"{path}: 读取失败 - {e}")
                continue

            language = infer_language_from_path(path=path)
            parsed = parse_file(path=path, content=content, language=language)
            n_sym = len(parsed.symbols)
            n_imp = len(parsed.imports)
            total_symbols += n_sym
            total_imports += n_imp
            chunk_symbols += n_sym
            file_details.append((path, n_sym, n_imp))

            for sym in parsed.symbols:
                if len(sym.code) > EMBED_MAX_CHARS:
                    oversized_symbols.append((path, sym.name, len(sym.code)))

        chunk_elapsed = time.monotonic() - chunk_t0
        print(
            f"  chunk {i + 1}/{len(chunks)}: "
            f"{len(chunk)} 文件, {chunk_symbols} symbols, "
            f"{chunk_elapsed:.2f}s"
        )

    total_time = time.monotonic() - t0

    # Step 3: 汇总报告
    print(f"\n{'=' * 60}")
    print(f"汇总")
    print(f"{'=' * 60}")
    print(f"文件总数:        {len(relative_paths)}")
    print(f"Symbol 总数:     {total_symbols}")
    print(f"Import 总数:     {total_imports}")
    print(f"解析错误:        {len(parse_errors)}")
    print(f"总耗时:          {total_time:.2f}s")

    # Embedding 预估
    from app.llm.embedding import EMBED_BATCH_SIZE, EMBED_CONCURRENCY
    symbol_batches = (total_symbols + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE if total_symbols else 0
    est_serial_seconds = symbol_batches * 1.0
    est_concurrent_seconds = symbol_batches * 1.0 / EMBED_CONCURRENCY

    print(f"\n📊 Embedding 预估 (仅 symbol 级):")
    print(f"  Symbol embedding: {total_symbols} 条 → {symbol_batches} 个 API 调用")
    print(f"  串行预估耗时:     ~{est_serial_seconds:.0f}s ({est_serial_seconds / 60:.1f} min)")
    print(f"  并发预估耗时:     ~{est_concurrent_seconds:.0f}s ({est_concurrent_seconds / 60:.1f} min) [concurrency={EMBED_CONCURRENCY}]")

    # 内存预估
    vec_memory_mb = total_symbols * 1536 * 8 / (1024 * 1024)
    print(f"\n💾 内存预估:")
    print(f"  全量积累向量内存:  ~{vec_memory_mb:.0f} MB (改进前)")
    per_chunk_symbols = total_symbols / len(chunks) if chunks else 0
    chunk_memory_mb = per_chunk_symbols * 1536 * 8 / (1024 * 1024)
    print(f"  单 chunk 向量内存: ~{chunk_memory_mb:.0f} MB (改进后, chunk_size={args.chunk_size})")

    if parse_errors:
        print(f"\n⚠️  解析错误:")
        for err in parse_errors[:20]:
            print(f"  {err}")
        if len(parse_errors) > 20:
            print(f"  ...还有 {len(parse_errors) - 20} 个")

    # 超长 symbol 统计
    if oversized_symbols:
        print(f"\n✂️  超长 symbol（将被截断, 阈值={EMBED_MAX_CHARS} 字符）: {len(oversized_symbols)} 个")
        oversized_symbols.sort(key=lambda x: -x[2])
        for path, name, chars in oversized_symbols[:20]:
            print(f"  {chars:>7,} chars | {name} @ {path}")
        if len(oversized_symbols) > 20:
            print(f"  ...还有 {len(oversized_symbols) - 20} 个")
    else:
        print(f"\n✅ 无超长 symbol（阈值={EMBED_MAX_CHARS} 字符）")

    # Top files by symbol count
    file_details.sort(key=lambda x: -x[1])
    print(f"\n🏆 Symbol 最多的文件 (Top 10):")
    for path, n_sym, n_imp in file_details[:10]:
        print(f"  {n_sym:4d} symbols | {path}")


def _print_file_list(paths: list[str]) -> None:
    print(f"\n文件列表 (前 50 个):")
    for p in paths[:50]:
        print(f"  {p}")
    if len(paths) > 50:
        print(f"  ...还有 {len(paths) - 50} 个")


if __name__ == "__main__":
    main()

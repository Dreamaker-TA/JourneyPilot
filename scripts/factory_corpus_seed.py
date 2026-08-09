#!/usr/bin/env python3
"""出厂语料种子的三个动作：导出、自检、灌库。

种子是什么、为什么本地种子可以包含向量、为什么身份不符要重算 —— 都写在
`src/travel_agent/rag/factory_seed.py` 的模块 docstring 里，**那里是定义处**，
这个文件只是它的命令行入口。

    python scripts/factory_corpus_seed.py --export          # 从当前库导出成 data/corpus/seed/
    python scripts/factory_corpus_seed.py --verify          # 只查文件，不连库，退出码 = 问题数
    python scripts/factory_corpus_seed.py --load            # 只补空集合（开机自举走的也是这条）
    python scripts/factory_corpus_seed.py --load --force    # 清空出厂集合后全量重灌

`--load` 不带 `--force` 时不会动任何已有内容：运营者用 `index_knowledge.py` 往出厂集合
里加过东西，无条件覆盖会把那些悄悄丢掉。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))


async def do_export() -> int:
    from travel_agent.infrastructure.database import init_db
    from travel_agent.rag.factory_seed import DEFAULT_SEED_DIR, export_seed

    await init_db()
    manifest = await export_seed()
    print(f"已导出 → {DEFAULT_SEED_DIR}")
    print(f"  embedder   : {manifest.embedder}")
    print(f"  chunk 总数 : {manifest.chunk_count}")
    for name, count in manifest.collections.items():
        print(f"    {name:<16} {count:>6}")
    vectors_mb = manifest.chunk_count * manifest.dimensions * 4 / 1024 / 1024
    print(f"  向量文件   : {vectors_mb:.1f} MB（{manifest.dimensions} 维 float32）")
    return 0


def do_verify() -> int:
    from travel_agent.rag.factory_seed import DEFAULT_SEED_DIR, read_manifest, verify_seed

    problems = verify_seed()
    manifest = read_manifest()
    if manifest is not None:
        print(f"种子目录 : {DEFAULT_SEED_DIR}")
        print(f"embedder : {manifest.embedder}")
        print(f"chunk    : {manifest.chunk_count}  {manifest.collections}")
    if problems:
        print(f"\n✗ {len(problems)} 个问题：")
        for problem in problems:
            print(f"  - {problem}")
        return len(problems)
    print("\n✓ 种子自检通过")
    return 0


async def do_load(force: bool) -> int:
    from travel_agent.infrastructure.database import init_db
    from travel_agent.rag.factory_seed import load_seed

    await init_db()
    report = await load_seed(force=force)
    if report.reembedded:
        print("⚠ 种子的 embedder 与当前配置不一致，已用当前 embedder 重算向量")
    if report.skipped_non_empty:
        print(f"跳过（库里已有内容，未覆盖）: {', '.join(report.skipped_non_empty)}")
    if report.loaded:
        for name, count in sorted(report.loaded.items()):
            print(f"灌入 {name:<16} {count:>6} 段")
    else:
        print("没有需要补的集合")
    print(f"当前库存: {report.present}")
    if report.problems:
        print(f"\n✗ {len(report.problems)} 个问题：")
        for problem in report.problems:
            print(f"  - {problem}")
        return len(report.problems)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export", action="store_true", help="从当前库导出种子")
    group.add_argument("--verify", action="store_true", help="只查种子文件本身（不连库）")
    group.add_argument("--load", action="store_true", help="把种子灌进库")
    parser.add_argument(
        "--force",
        action="store_true",
        help="--load 专用：先清空出厂集合再全量重灌（会丢掉运营者后加的内容）",
    )
    args = parser.parse_args()

    if args.verify:
        return do_verify()
    if args.export:
        return asyncio.run(do_export())
    return asyncio.run(do_load(args.force))


if __name__ == "__main__":
    raise SystemExit(main())

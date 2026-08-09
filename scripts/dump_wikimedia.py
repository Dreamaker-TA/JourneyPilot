"""
Wikimedia 双源 dump 工具 — 把 Wikipedia / Wikivoyage 中文条目拉成本地 JSON。

设计原则：
1. dump-only — 不入库，只落本地 JSON；入库由 scripts/index_knowledge.py 负责
2. 双源支持 — Wikipedia (百科风) + Wikivoyage (攻略风) 两种叙事
3. 走 MediaWiki Action API (`prop=extracts&explaintext=1`)，返回纯文本，无需 HTML 解析
4. 单条记录字段对齐 retrieval-set v0.1 的 doc_source 命名约定

使用方式：
  # 拉 Wikipedia 中文 30 篇热门目的地
  python scripts/dump_wikimedia.py --project wikipedia --lang zh \\
      --titles-file data/wiki_titles.txt --output data/raw_wikipedia

  # 拉 Wikivoyage 中文 20 篇旅游攻略
  python scripts/dump_wikimedia.py --project wikivoyage --lang zh \\
      --titles-file data/wikivoyage_titles.txt --output data/raw_wikivoyage

输出结构：
  data/raw_wikipedia/
    wiki-zh-杭州市.json
    wiki-zh-成都市.json
    ...
    _manifest.json             # 索引文件，汇总所有 dump 产物
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx


REQUEST_TIMEOUT = 30.0
REQUEST_DELAY = 1.0  # 节流，符合 Wikimedia 公允使用（0.3 实测仍会被限速）
# Wikimedia 的 UA 政策要求客户端自报身份与用途，否则按匿名爬虫处理。原先那句
# "JourneyPilot/1.0 (research bot)" 正是被处理的形状：实测同一秒内，它拿回的是一个
# 200 的非 JSON 拦截页（在下面会变成「异常」）或 429，而换成这一句立刻拿到正文。
# 这不是限速问题——降速与退避都救不了它，只有把身份说清楚才行。
USER_AGENT = (
    "JourneyPilot-corpus-dump/1.0 "
    "(JourneyPilot knowledge-base corpus builder; local research use) httpx"
)

# Wikimedia 对匿名客户端限速，超了就整串 429。原先没有退避：一旦撞上，剩下的标题
# 会一条不落地全部「失败」，而 manifest 只记 failed，读起来像「这些条目不存在」。
# 65 条一批实测 9 条之后开始连续 429，55 条全废——所以退避不是锦上添花。
RETRY_AFTER_DEFAULT_SECONDS = 60.0
RETRY_BACKOFF_SECONDS = (5.0, 20.0, 60.0, 120.0)

# project → doc_source 前缀（用于 retrieval-set 标注时拼接 doc_source）
SOURCE_PREFIX = {
    "wikipedia": "wiki",
    "wikivoyage": "voyage",
}


def build_api_url(project: str, lang: str) -> str:
    """构造 MediaWiki Action API endpoint。"""
    if project not in SOURCE_PREFIX:
        raise ValueError(f"不支持的 project: {project}; 仅支持 {list(SOURCE_PREFIX)}")
    return f"https://{lang}.{project}.org/w/api.php"


async def fetch_extract(
    client: httpx.AsyncClient, api_url: str, title: str, variant: Optional[str] = None
) -> Optional[str]:
    """调 MediaWiki API 拉某条目的纯文本 extract。

    返回 None 表示拉取失败 / 页面不存在。
    """
    params = {
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "explaintext": 1,
        "titles": title,
        "redirects": 1,
    }
    # 中文维基按条目自己的字形出正文，日本相关条目多为繁体。语料库其余部分全是简体，
    # 混进繁体既让 embedding / BM25 面对两种字形，也让所有按字面比对名字的东西
    # （提名度量的后缀词表、名字一致性）直接看不见它们——「明治神宮」提不出任何提名。
    if variant:
        params["variant"] = variant
    data = None
    for attempt, backoff in enumerate((*RETRY_BACKOFF_SECONDS, None)):
        try:
            resp = await client.get(api_url, params=params)
            resp.raise_for_status()
            data = resp.json()
            break
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # 只对「稍后再来」这一类重试；404 / 400 重试多少次都是同一个答案。
            if status not in (429, 500, 502, 503, 504) or backoff is None:
                print(f"  ✗ HTTP {status}: {title}")
                return None
            delay = backoff
            header = e.response.headers.get("Retry-After")
            if header:
                try:
                    delay = max(delay, float(header))
                except ValueError:
                    delay = max(delay, RETRY_AFTER_DEFAULT_SECONDS)
            print(f"  … HTTP {status}，第 {attempt + 1} 次退避 {delay:.0f}s: {title}")
            await asyncio.sleep(delay)
        except ValueError as e:
            # 200 但正文不是 JSON —— 上游返回的是拦截页/错误页，和 429 同类，可重试。
            if backoff is None:
                print(f"  ✗ 非 JSON 响应 [{title}]: {e}")
                return None
            print(f"  … 非 JSON 响应，第 {attempt + 1} 次退避 {backoff:.0f}s: {title}")
            await asyncio.sleep(backoff)
        except Exception as e:
            print(f"  ✗ 异常 [{title}]: {e}")
            return None
    if data is None:
        return None

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        return None
    page = next(iter(pages.values()))
    if "missing" in page:
        print(f"  ✗ 页面不存在: {title}")
        return None
    extract = page.get("extract", "").strip()
    return extract or None


def clean_extract(text: str) -> str:
    """轻量清洗：归并空白、去掉残留 wiki 标记。

    MediaWiki API 的 explaintext=1 已经把内容转为纯文本，但仍可能残留：
    - 多余空行
    - 章节标题前的多余空格
    - `__NOEDITSECTION__` 等 magic words
    """
    # 去掉 magic words
    text = re.sub(r"__[A-Z]+__", "", text)
    # 多个连续空行 → 单空行
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    # 行尾空白
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


async def dump_titles(
    titles: list[str],
    project: str,
    lang: str,
    output_dir: Path,
    min_chars: int,
    variant: Optional[str] = None,
) -> dict:
    """批量 dump 一组 titles 到 output_dir，并写 _manifest.json。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    api_url = build_api_url(project, lang)
    prefix = SOURCE_PREFIX[project]

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as client:
        manifest_items = []
        too_short = []
        failed = []

        for idx, title in enumerate(titles, 1):
            doc_source = f"{prefix}-{lang}-{title}"
            # 文件名安全：把 / 等替换为 _
            safe_name = re.sub(r"[\\/:*?\"<>|]", "_", doc_source)
            outfile = output_dir / f"{safe_name}.json"
            # 断点续传：上游限速时一批 65 条要分几次才拉得完，重跑不该把已经拿到的
            # 正文再要一遍——那既慢又是又一次挨限速的机会。
            if outfile.exists():
                cached = json.loads(outfile.read_text(encoding="utf-8"))
                if cached.get("content"):
                    manifest_items.append(
                        {
                            "doc_source": cached["doc_source"],
                            "title": cached["title"],
                            "char_count": cached["char_count"],
                            "status": cached.get("status", "ok"),
                            "file": outfile.name,
                        }
                    )
                    if cached.get("status") == "too_short":
                        too_short.append(
                            {"title": title, "char_count": cached["char_count"]}
                        )
                    print(f"[{idx}/{len(titles)}] 复用已有 dump: {title}")
                    continue

            print(f"[{idx}/{len(titles)}] 拉取 {project}-{lang}: {title}")
            raw = await fetch_extract(client, api_url, title, variant)
            if raw is None:
                failed.append(title)
                await asyncio.sleep(REQUEST_DELAY)
                continue

            content = clean_extract(raw)
            char_count = len(content)
            source_url = f"https://{lang}.{project}.org/wiki/{title}"

            record = {
                "doc_source": doc_source,
                "title": title,
                "project": project,
                "lang": lang,
                "source_url": source_url,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "content": content,
                "char_count": char_count,
                "license": "CC-BY-SA-3.0 (Wikimedia)",
            }
            if char_count < min_chars:
                record["status"] = "too_short"
                too_short.append({"title": title, "char_count": char_count})
                print(f"  ⚠ 字数 {char_count} < {min_chars}，标记 too_short")
            else:
                record["status"] = "ok"

            outfile.write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            manifest_items.append(
                {
                    "doc_source": doc_source,
                    "title": title,
                    "char_count": char_count,
                    "status": record["status"],
                    "file": outfile.name,
                }
            )
            print(f"  ✓ {char_count} 字 → {outfile.name}")
            await asyncio.sleep(REQUEST_DELAY)

    manifest = {
        "project": project,
        "lang": lang,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_requested": len(titles),
        "total_ok": len([m for m in manifest_items if m["status"] == "ok"]),
        "total_too_short": len(too_short),
        "total_failed": len(failed),
        "min_chars_threshold": min_chars,
        "items": manifest_items,
        "too_short": too_short,
        "failed": failed,
    }
    (output_dir / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n=== {project}-{lang} 完成 ===")
    print(f"  成功      : {manifest['total_ok']}")
    print(f"  字数不足  : {manifest['total_too_short']}")
    print(f"  拉取失败  : {manifest['total_failed']}")
    print(f"  manifest  : {output_dir / '_manifest.json'}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wikimedia 双源 (Wikipedia / Wikivoyage) dump 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--project",
        required=True,
        choices=["wikipedia", "wikivoyage"],
        help="Wikimedia 项目（wikipedia 百科 / wikivoyage 旅游攻略）",
    )
    parser.add_argument("--lang", default="zh", help="语言代码（默认: zh）")
    parser.add_argument(
        "--titles-file",
        required=True,
        type=Path,
        help="标题列表文件，每行一个；# 开头为注释，空行忽略",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="输出目录（会自动创建）",
    )
    parser.add_argument(
        "--variant",
        default=None,
        help=(
            "中文字形变体，例如 zh-cn（简体）。中文维基按条目自己的字形出正文，"
            "不指定就会把繁体正文混进简体语料库"
        ),
    )
    parser.add_argument(
        "--min-chars",
        type=int,
        default=500,
        help="最少字符数门槛，低于则标记 too_short（默认: 500）",
    )

    args = parser.parse_args()

    if not args.titles_file.exists():
        print(f"错误：titles 文件不存在 {args.titles_file}", file=sys.stderr)
        return 1

    titles = [
        line.strip()
        for line in args.titles_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not titles:
        print(f"错误：titles 文件为空 {args.titles_file}", file=sys.stderr)
        return 1

    print(f"准备拉取 {len(titles)} 个 {args.project}-{args.lang} 条目")
    asyncio.run(
        dump_titles(
            titles, args.project, args.lang, args.output, args.min_chars, args.variant
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

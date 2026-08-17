"""
知识库数据导入 CLI 工具

使用方式：
  # 从 Wikipedia 批量导入目的地摘要（一篇一个块；要整篇正文见下面的 --from-jsondir）
  python scripts/index_knowledge.py --wikipedia 日本 京都 东京 泰国 --collection destinations

  # 从 Wikipedia 导入所有预定义热门目的地（约 80 个）
  python scripts/index_knowledge.py --wikipedia-popular --collection destinations

  # 从 Wikitravel 爬取旅行攻略
  python scripts/index_knowledge.py --url https://wikitravel.org/zh/日本 --collection travel_tips

  # 从本地文件目录批量索引
  python scripts/index_knowledge.py --dir ./knowledge_base --collection destinations

  # 从单个本地文件索引
  python scripts/index_knowledge.py --file ./data/japan_guide.md --collection destinations

  # 从 dump_wikimedia.py 输出目录批量索引（推荐：三档对照实验入库主路径）
  python scripts/index_knowledge.py --from-jsondir data/raw_wikipedia data/raw_wikivoyage \\
      --collection knowledge_contextual --chunker-type contextual

  # 查看所有集合的统计信息
  python scripts/index_knowledge.py --stats

集合说明（旧路径）：
  destinations  - 目的地基础信息（来源：Wikipedia）
  visa_policies - 签证要求、入境政策（来源：Wikitravel）
  travel_tips   - 实用攻略、预算参考（来源：Wikitravel）
  local_culture - 文化习俗、注意事项（来源：Wikipedia/Wikitravel）

RAG 实验对照路径（v1.1 retrieval-set v0.1）：
  knowledge_text       - TextChunker 入库（固定窗口分块）
  knowledge_semantic   - SemanticChunker 入库（语义边界分块）
  knowledge_contextual - ContextualChunker 入库（Anthropic Contextual Retrieval）
"""

import asyncio
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parents[1]))

from travel_agent.rag.collections import FACTORY_KNOWLEDGE_COLLECTIONS  # noqa: E402

# 出厂集合名只有一处定义。这里此前抄了一份写死的四集合清单（--stats 用），
# 与 `rag/collections.py` 靠上面那段 docstring 手工同步 —— 清单一漂，运维照着
# `--stats` 读到的「知识库统计」就少一个集合。这个常量直接复用唯一定义处。
#
# 注意这里用的是**全部**出厂集合名，不是 `seeded_factory_collections()`：
# `--stats` 要的正是「每个出厂集合各有几段」，一个 0 段的集合印成「(空)」是它的答案。
COLLECTIONS = FACTORY_KNOWLEDGE_COLLECTIONS


async def index_wikipedia(titles: list, collection: str) -> None:
    from travel_agent.db.report import require_database_contract
    from travel_agent.rag.sources.wikipedia_fetcher import WikipediaFetcher

    await require_database_contract()

    fetcher = WikipediaFetcher(lang="zh")
    print(f"开始从中文 Wikipedia 抓取 {len(titles)} 个条目...")
    result = await fetcher.fetch_and_index(
        titles,
        collection=collection,
        content_type="destination",
    )
    await fetcher.close()
    print(f"完成：索引 {result['indexed']} 个文本块，失败 {result['failed']} 个")


async def index_wikipedia_popular(collection: str) -> None:
    from travel_agent.db.report import require_database_contract
    from travel_agent.rag.sources.wikipedia_fetcher import (
        WikipediaFetcher,
        POPULAR_DESTINATIONS_ZH,
    )

    await require_database_contract()

    fetcher = WikipediaFetcher(lang="zh")
    print(f"开始从中文 Wikipedia 抓取 {len(POPULAR_DESTINATIONS_ZH)} 个热门目的地...")
    result = await fetcher.fetch_and_index(
        POPULAR_DESTINATIONS_ZH,
        collection=collection,
        content_type="destination",
    )
    await fetcher.close()
    print(
        f"完成：处理 {len(POPULAR_DESTINATIONS_ZH)} 个目的地，"
        f"索引 {result['indexed']} 个文本块，失败 {result['failed']} 个"
    )


async def index_urls(urls: list, collection: str) -> None:
    from travel_agent.db.report import require_database_contract
    from travel_agent.rag.sources.web_crawler import WebCrawler

    await require_database_contract()

    crawler = WebCrawler()
    print(f"开始爬取 {len(urls)} 个 URL...")
    result = await crawler.crawl_and_index(urls, collection=collection)
    await crawler.close()
    print(f"完成：索引 {result['indexed']} 个文本块，失败 {result['failed']} 个")


async def index_directory(directory: str, collection: str) -> None:
    from travel_agent.db.report import require_database_contract
    from travel_agent.rag.sources.document_loader import DocumentLoader

    await require_database_contract()

    loader = DocumentLoader()
    print(f"开始索引目录：{directory}")
    result = await loader.load_directory(directory, collection=collection)
    print(f"完成：处理 {result['files']} 个文件，索引 {result['indexed']} 个文本块，失败 {result['failed']} 个")


async def index_file(file_path: str, collection: str) -> None:
    from travel_agent.db.report import require_database_contract
    from travel_agent.rag.sources.document_loader import DocumentLoader

    await require_database_contract()

    loader = DocumentLoader()
    print(f"开始索引文件：{file_path}")
    count = await loader.load_file(file_path, collection=collection)
    print(f"完成：索引 {count} 个文本块")


def resolve_default_chunker_type() -> str:
    """The chunker the project configured, for callers that did not name one."""
    from travel_agent.config import get_settings

    return get_settings().rag.chunker_type


async def index_from_jsondirs(
    jsondirs: list,
    collection: str,
    chunker_type: str,
    drop_existing: bool = False,
) -> None:
    """
    从 dump_wikimedia.py 输出的 JSON 目录批量入库（RAG 实验对照主路径）。

    每个目录下必须有 _manifest.json + 多个 {doc_source}.json 文件。
    每条 JSON 必含 `doc_source` / `content` / `title` 字段；可选 `project` / `lang` 等。

    入库时 source 字段 = doc_source；metadata 携带 doc_source/title/project/lang，
    chunker 由 --chunker-type 指定（text/semantic/contextual），
    collection 由 --collection 指定（建议 knowledge_text/semantic/contextual）。
    """
    from travel_agent.db.report import require_database_contract
    from travel_agent.rag.indexer import KnowledgeIndexer

    await require_database_contract()
    indexer = KnowledgeIndexer(chunker_type=chunker_type)

    if drop_existing:
        deleted = await indexer.delete_collection(collection)
        print(f"已清空 collection='{collection}'（删除 {deleted} 个旧 chunk）")

    docs = []
    for jsondir_str in jsondirs:
        jsondir = Path(jsondir_str)
        if not jsondir.is_dir():
            print(f"⚠ 跳过：{jsondir} 不是目录")
            continue

        manifest_path = jsondir / "_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            file_names = [m["file"] for m in manifest["items"] if m.get("status") == "ok"]
        else:
            file_names = sorted(
                f.name for f in jsondir.glob("*.json") if not f.name.startswith("_")
            )

        for fname in file_names:
            payload = json.loads((jsondir / fname).read_text(encoding="utf-8"))
            if not payload.get("content"):
                continue
            docs.append({
                "content": payload["content"],
                "source": payload["doc_source"],
                "metadata": {
                    "doc_source": payload["doc_source"],
                    "title": payload.get("title", payload["doc_source"]),
                    "project": payload.get("project", ""),
                    "lang": payload.get("lang", ""),
                    "source_url": payload.get("source_url", ""),
                    "license": payload.get("license", ""),
                },
            })

    print(
        f"准备入库 {len(docs)} 个文档 → collection='{collection}', chunker='{chunker_type}'"
    )
    if not docs:
        print("⚠ 没有可入库的文档，退出")
        return

    count = await indexer.index_documents(docs, collection=collection)
    stats = await indexer.get_collection_stats(collection)
    print(f"完成：索引 {count} 个 chunk")
    print(f"集合统计：total={stats.get('total')}, sources={stats.get('sources')}")


async def show_stats() -> None:
    from travel_agent.db.report import require_database_contract
    from travel_agent.rag.indexer import KnowledgeIndexer

    await require_database_contract()
    indexer = KnowledgeIndexer()

    print("\n知识库统计：")
    print(f"{'集合名':<20} {'块数':>8} {'来源数':>8} {'最早':>20} {'最新':>20}")
    print("-" * 80)
    for col in COLLECTIONS:
        stats = await indexer.get_collection_stats(col)
        if stats.get("total"):
            print(
                f"{col:<20} {stats['total']:>8} {stats['sources']:>8} "
                f"{str(stats.get('oldest', ''))[:19]:>20} "
                f"{str(stats.get('newest', ''))[:19]:>20}"
            )
        else:
            print(f"{col:<20} {'(空)':>8}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="JourneyPilot 知识库数据导入工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--wikipedia", "-w",
        nargs="+",
        metavar="TITLE",
        help="从中文 Wikipedia 批量抓取指定目的地（例：日本 京都 泰国）",
    )
    group.add_argument(
        "--wikipedia-popular",
        action="store_true",
        help="从中文 Wikipedia 抓取所有预定义热门目的地（约 80 个）",
    )
    group.add_argument(
        "--url",
        nargs="+",
        help="从指定 URL 爬取并索引（可多个）",
    )
    group.add_argument(
        "--dir",
        metavar="DIRECTORY",
        help="批量索引本地目录中的文档",
    )
    group.add_argument(
        "--file",
        metavar="FILE",
        help="索引单个本地文件",
    )
    group.add_argument(
        "--from-jsondir",
        nargs="+",
        metavar="DIR",
        help="从 dump_wikimedia.py 输出的 JSON 目录批量入库（可多个目录拼合）",
    )
    group.add_argument(
        "--stats",
        action="store_true",
        help="显示所有知识库集合的统计信息",
    )

    parser.add_argument(
        "--collection", "-c",
        default="destinations",
        help="目标知识库集合（默认: destinations；RAG 对照建议: knowledge_text / knowledge_semantic / knowledge_contextual）",
    )
    parser.add_argument(
        "--chunker-type",
        default=None,
        choices=["text", "semantic", "contextual"],
        help="chunker 策略（默认读 config.rag.chunker_type；--from-jsondir 时建议显式指定）",
    )
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="入库前清空目标 collection（--from-jsondir 专用）",
    )
    args = parser.parse_args()

    if args.stats:
        asyncio.run(show_stats())
    elif args.wikipedia:
        asyncio.run(index_wikipedia(args.wikipedia, args.collection))
    elif args.wikipedia_popular:
        asyncio.run(index_wikipedia_popular(args.collection))
    elif args.url:
        asyncio.run(index_urls(args.url, args.collection))
    elif args.dir:
        asyncio.run(index_directory(args.dir, args.collection))
    elif args.file:
        asyncio.run(index_file(args.file, args.collection))
    elif args.from_jsondir:
        # 说到就要做到：--help 与下面这行告警都承诺「未指定就读 config」，所以这里**不许
        # 硬编码**一种分块策略。分块策略是检索质量的最大单一变量，默认值悄悄换掉一种策略，
        # 建出来的语料库就和配置声明的不是同一份东西。
        chunker_type = args.chunker_type or resolve_default_chunker_type()
        if not args.chunker_type:
            print("⚠ --from-jsondir 建议显式 --chunker-type {text|semantic|contextual}；")
            print(f"  未指定，已读 config.rag.chunker_type = {chunker_type!r}")
        asyncio.run(
            index_from_jsondirs(
                args.from_jsondir,
                args.collection,
                chunker_type,
                drop_existing=args.drop_existing,
            )
        )


if __name__ == "__main__":
    main()

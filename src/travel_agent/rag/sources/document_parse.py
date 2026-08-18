"""上传文档的输入边界与解析入口。

一份上传要过三道界，顺序不能换：

1. **类型**：后缀在支持范围内，且内容与后缀相符（PDF 看 magic bytes，DOCX 看
   ZIP 结构与必要条目）。只看后缀等于让调用方声明自己的类型。
2. **规模**：字节数、ZIP 解压后总量与压缩比、条目数。DOCX 是 ZIP，一份 200 KB
   的上传展开后可以是几个 GB，而这一道必须在**把它读进内存之前**判完。
3. **解析**：交给受限子进程（`document_parser_worker.py`），带 CPU / 地址空间
   上限与墙钟上限，超时杀进程组。

只有第 3 道会花时间，前两道都是常数级的（ADR-0006）。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ...config import IngestConfig, get_settings
from ...utils.user_text import content_length
from ...services.blocking_work import (
    BlockingWorkBusy,
    blocking_channel,
    run_blocking,
    run_in_thread,
)

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".pdf", ".docx"})

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
# 一个 OOXML 文档必须有的两个条目。少了它们的 ZIP 是别的东西，不是 DOCX。
_DOCX_REQUIRED_ENTRIES = ("[Content_Types].xml", "word/document.xml")

_WORKER = Path(__file__).with_name("document_parser_worker.py")


class DocumentRejected(Exception):
    """这份文件不进库，以及是哪一种不进。

    ``code`` 是产品合同的一部分：界面按它说话（前端那张表在
    `frontend/src/lib/knowledgeIngestFailure.ts`，两张表逐条对齐）。
    ``detail`` 只给日志 —— 解析器的原话对旅行者没有意义。
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    #: PDF 的页数；其余格式为 None。
    pages: Optional[int]
    #: 正文是否在 ``max_extracted_chars`` 处被截断。
    truncated: bool


def _reject(code: str, detail: str) -> DocumentRejected:
    logger.warning("上传被拒 | code=%s detail=%s", code, detail)
    return DocumentRejected(code, detail)


def require_supported_suffix(suffix: str) -> str:
    normalized = (suffix or "").lower()
    if normalized not in SUPPORTED_SUFFIXES:
        raise _reject("unsupported_file_type", f"suffix {normalized!r} not supported")
    return normalized


def _check_zip_bomb(raw: bytes, config: IngestConfig) -> None:
    """按条目数、解压总量和压缩比判一份 ZIP，不解压任何一个条目。

    判据取自 ZIP 中央目录里的 ``file_size``/``compress_size``。它可以被伪造，但
    伪造成「小」的那一份在解析阶段会被子进程的 RLIMIT 拦住 —— 这一道要挡的是
    诚实声明自己有几个 GB 的那一类，那一类不该被读进内存。
    """

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise _reject("document_unreadable", f"not a zip container: {exc}") from exc

    missing = [name for name in _DOCX_REQUIRED_ENTRIES if name not in names]
    if missing:
        raise _reject(
            "document_unreadable",
            f"zip is not an OOXML document; missing {', '.join(missing)}",
        )
    if len(infos) > config.max_docx_entries:
        raise _reject(
            "document_too_complex",
            f"{len(infos)} zip entries (max {config.max_docx_entries})",
        )
    uncompressed = sum(int(info.file_size) for info in infos)
    if uncompressed > config.max_uncompressed_bytes:
        raise _reject(
            "document_too_complex",
            f"expands to {uncompressed} bytes (max {config.max_uncompressed_bytes})",
        )
    compressed = sum(int(info.compress_size) for info in infos) or 1
    ratio = uncompressed / compressed
    if ratio > config.max_compression_ratio:
        raise _reject(
            "document_too_complex",
            f"compression ratio {ratio:.1f} (max {config.max_compression_ratio})",
        )


def inspect_upload(raw: bytes, suffix: str, *, config: Optional[IngestConfig] = None) -> str:
    """把一份上传过完前两道界，返回归一化后的后缀。

    在解析之前调用；调用方拿到的是「可以花时间去解析它了」这个结论。
    """

    settings = config or get_settings().ingest
    normalized = require_supported_suffix(suffix)
    if len(raw) > settings.max_upload_bytes:
        raise _reject(
            "file_too_large",
            f"{len(raw)} bytes exceeds {settings.max_upload_bytes}",
        )
    if not raw:
        raise _reject("no_indexable_text", "empty upload")
    if normalized == ".pdf":
        if not raw.startswith(_PDF_MAGIC):
            raise _reject("document_unreadable", "missing %PDF- header")
    elif normalized == ".docx":
        if not raw.startswith(_ZIP_MAGIC):
            raise _reject("document_unreadable", "missing zip local file header")
        _check_zip_bomb(raw, settings)
    return normalized


#: 一份「进得去」的资料至少要有这么多非空白字符。**这个数只写一次**：它必须对四种
#: 格式、对上传与批量导入两条入口一视同仁 —— 否则扫描版 PDF 在一条路上被拒、在另一条
#: 路上变成一次「成功索引 0 个文本块」。
MIN_INDEXABLE_CHARS = 8


def has_indexable_text(text: str) -> bool:
    """这份正文够不够进知识库。空白不算字符。"""

    return content_length(text) >= MIN_INDEXABLE_CHARS


def _decode_text(raw: bytes, config: IngestConfig) -> ParsedDocument:
    text = raw.decode("utf-8", errors="replace")
    if len(text) > config.max_extracted_chars:
        return ParsedDocument(
            text=text[: config.max_extracted_chars], pages=None, truncated=True
        )
    return ParsedDocument(text=text, pages=None, truncated=False)


class _ParserOutputTooLarge(Exception):
    """worker 的 stdout 超过了上界。"""


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """杀掉整个进程组：解析器可能已经 fork 出别的东西。

    ``start_new_session=True`` 保证子进程就是会话首领，于是 pgid == pid。**不查
    ``getpgid``**：子进程如果还没成为首领，那个调用返回的是 API 进程自己的进程组，
    这一枪就打在 uvicorn 身上。
    """

    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:  # pragma: no cover - 内核回收不了的僵尸
        logger.error("解析子进程未能回收 | pid=%s", process.pid)


async def _read_capped(stream: Optional[asyncio.StreamReader], cap: int) -> bytes:
    """按块读到 EOF，超过 ``cap`` 就抛。

    ``communicate()`` 用的是 ``read(-1)``，它把整条流拼起来，``limit=`` 对它无效
    （``LimitOverrunError`` 只从 readline/readuntil 抛）—— 也就是说那个上界根本不存在。
    """

    if stream is None:
        return b""
    chunks: List[bytes] = []
    total = 0
    while True:
        block = await stream.read(65536)
        if not block:
            break
        total += len(block)
        if total > cap:
            raise _ParserOutputTooLarge()
        chunks.append(block)
    return b"".join(chunks)


async def _run_parser_subprocess(
    suffix: str, path: str, config: IngestConfig
) -> ParsedDocument:
    limits = json.dumps(
        {
            "max_pdf_pages": config.max_pdf_pages,
            "max_extracted_chars": config.max_extracted_chars,
            "parse_cpu_seconds": config.parse_cpu_seconds,
            "parse_address_space_bytes": config.parse_address_space_bytes,
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        str(_WORKER),
        suffix,
        path,
        limits,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # setsid 走这个参数而不是 preexec_fn：后者在多线程进程里 fork 之后跑，
        # CPython 明说不安全，而这个进程有 anyio 工作线程。RLIMIT 由 worker 自己装。
        start_new_session=True,
        # 解析器不需要网络也不需要仓库环境，但它需要 site-packages，所以继承
        # 当前解释器的环境而不是清空它。
        cwd=tempfile.gettempdir(),
    )
    # stdout 已被 worker 按 max_extracted_chars 截断；这个上界防的是它没截住。
    stdout_cap = config.max_extracted_chars * 4 + 64 * 1024
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(
                _read_capped(process.stdout, stdout_cap),
                _read_capped(process.stderr, 64 * 1024),
            ),
            timeout=config.parse_timeout_seconds,
        )
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError as exc:
        await _kill_process_group(process)
        raise _reject(
            "document_parse_timeout",
            f"parser exceeded {config.parse_timeout_seconds}s",
        ) from exc
    except _ParserOutputTooLarge as exc:
        await _kill_process_group(process)
        raise _reject("document_too_complex", "parser output exceeded its limit") from exc

    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="replace")[:500]
        raise _reject(
            "document_unreadable",
            f"parser exited {process.returncode}: {detail}",
        )
    try:
        payload = json.loads((stdout or b"").decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise _reject("document_unreadable", f"parser produced no verdict: {exc}") from exc
    if not payload.get("ok"):
        raise _reject(
            str(payload.get("code") or "document_unreadable"),
            str(payload.get("detail") or "parser rejected the document"),
        )
    return ParsedDocument(
        text=str(payload.get("text") or ""),
        pages=payload.get("pages"),
        truncated=bool(payload.get("truncated")),
    )


async def parse_upload(raw: bytes, suffix: str) -> ParsedDocument:
    """把一份上传的字节解析成正文。前两道界在这里一并过完。"""

    config = get_settings().ingest
    normalized = inspect_upload(raw, suffix, config=config)
    if normalized in {".txt", ".md"}:
        # 纯文本不需要子进程：解码是 stdlib，输入上限已经判过。
        return _decode_text(raw, config)

    directory = tempfile.mkdtemp(prefix="journeypilot-ingest-")
    path = os.path.join(directory, f"upload{normalized}")
    try:
        # 落盘与解析共占**同一个**通道位置：「同时解析几份」只能有一个上限，
        # 而这两步属于同一份文档的同一次解析。
        async with blocking_channel("document_parse"):
            # 落盘也是同步 IO，10 MB 在 Event Loop 上写会卡住保活帧。
            await run_in_thread(Path(path).write_bytes, raw)
            return await _run_parser_subprocess(normalized, path, config)
    except BlockingWorkBusy as exc:
        raise _reject("ingest_busy", str(exc)) from exc
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def parse_file(path: Path) -> ParsedDocument:
    """解析本地磁盘上的一份文档（批量导入那一路），走同一组界。

    大小在**读进内存之前**判：一个 8 GB 文件先读完再判 file_too_large，判到的时候
    进程已经死了。给「本地目录」留这条旁路等于让它成为绕过全部边界的入口。
    """

    config = get_settings().ingest
    try:
        size = await run_blocking("document_parse", lambda: path.stat().st_size)
    except OSError as exc:
        raise _reject("document_unreadable", f"cannot stat {path.name}: {exc}") from exc
    if size > config.max_upload_bytes:
        raise _reject(
            "file_too_large",
            f"{path.name} is {size} bytes (max {config.max_upload_bytes})",
        )
    raw = await run_blocking("document_parse", path.read_bytes)
    return await parse_upload(raw, path.suffix)

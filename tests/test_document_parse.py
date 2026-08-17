"""上传解析的输入边界：类型、规模、zip bomb、页数、超时、子进程隔离。"""

from __future__ import annotations

import io
import zipfile

import pytest

from travel_agent.config import get_settings
from travel_agent.rag.sources.document_parse import (
    DocumentRejected,
    inspect_upload,
    parse_upload,
)
from travel_agent.utils.concurrency import reset_channels


@pytest.fixture(autouse=True)
def _isolate_channels():
    reset_channels()
    yield
    reset_channels()


@pytest.fixture
def ingest():
    config = get_settings().ingest
    original = config.model_dump()
    yield config
    for key, value in original.items():
        setattr(config, key, value)


def _docx(paragraphs: list[str]) -> bytes:
    """用 python-docx 造一份真的 DOCX（不手搓 ZIP：手搓的那份测不到解析器）。"""

    from docx import Document

    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pdf(pages: int, text: str = "JourneyPilot 行程资料") -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen.canvas import Canvas

    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=A4)
    for index in range(pages):
        canvas.drawString(72, 720, f"{text} p{index + 1}")
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def _zip_bomb(*, entries: int = 1, size: int = 200 * 1024 * 1024) -> bytes:
    """一份声明自己展开后很大的 DOCX。压缩比是判据，所以内容用可压缩的零。"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        for index in range(entries):
            archive.writestr(f"word/bomb{index}.bin", b"\0" * size)
    return buffer.getvalue()


# --- 类型 ---------------------------------------------------------------- #


def test_unsupported_suffix_is_rejected_by_code():
    with pytest.raises(DocumentRejected) as exc:
        inspect_upload(b"anything", ".exe")
    assert exc.value.code == "unsupported_file_type"


def test_a_pdf_suffix_over_non_pdf_bytes_is_rejected():
    """后缀说是 PDF、内容不是：这一道必须靠 magic bytes，不靠解析器报错。"""

    with pytest.raises(DocumentRejected) as exc:
        inspect_upload("这其实是一段纯文本".encode("utf-8"), ".pdf")
    assert exc.value.code == "document_unreadable"


def test_a_docx_suffix_over_non_zip_bytes_is_rejected():
    with pytest.raises(DocumentRejected) as exc:
        inspect_upload(b"not a zip at all", ".docx")
    assert exc.value.code == "document_unreadable"


def test_a_zip_without_ooxml_entries_is_not_a_docx():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hi")
    with pytest.raises(DocumentRejected) as exc:
        inspect_upload(buffer.getvalue(), ".docx")
    assert exc.value.code == "document_unreadable"


# --- 规模 ---------------------------------------------------------------- #


def test_oversize_upload_is_rejected_before_parsing(ingest):
    ingest.max_upload_bytes = 1024
    with pytest.raises(DocumentRejected) as exc:
        inspect_upload(b"x" * 2048, ".txt")
    assert exc.value.code == "file_too_large"


def test_zip_bomb_is_rejected_on_expanded_size(ingest):
    ingest.max_uncompressed_bytes = 1024 * 1024
    with pytest.raises(DocumentRejected) as exc:
        inspect_upload(_zip_bomb(), ".docx")
    assert exc.value.code == "document_too_complex"


def test_zip_bomb_is_rejected_on_compression_ratio(ingest):
    # 放开体积上限，只留比值那一道：一份 200 MB 的零压缩后不到 200 KB。
    ingest.max_uncompressed_bytes = 1024 ** 4
    ingest.max_compression_ratio = 50
    with pytest.raises(DocumentRejected) as exc:
        inspect_upload(_zip_bomb(), ".docx")
    assert exc.value.code == "document_too_complex"


def test_too_many_zip_entries_is_rejected(ingest):
    ingest.max_docx_entries = 3
    payload = _zip_bomb(entries=8, size=16)
    with pytest.raises(DocumentRejected) as exc:
        inspect_upload(payload, ".docx")
    assert exc.value.code == "document_too_complex"


# --- 解析 ---------------------------------------------------------------- #


async def test_a_real_docx_parses_in_a_subprocess():
    parsed = await parse_upload(_docx(["第一段正文", "第二段正文"]), ".docx")
    assert "第一段正文" in parsed.text
    assert "第二段正文" in parsed.text
    assert parsed.truncated is False


async def test_a_real_pdf_parses_and_reports_its_page_count():
    parsed = await parse_upload(_pdf(3), ".pdf")
    assert parsed.pages == 3
    assert "JourneyPilot" in parsed.text


async def test_a_pdf_over_the_page_cap_is_rejected(ingest):
    ingest.max_pdf_pages = 2
    with pytest.raises(DocumentRejected) as exc:
        await parse_upload(_pdf(5), ".pdf")
    assert exc.value.code == "document_too_complex"


async def test_extracted_text_is_truncated_at_the_char_cap(ingest):
    ingest.max_extracted_chars = 1024
    parsed = await parse_upload(_docx(["很长的一段" * 500]), ".docx")
    assert parsed.truncated is True
    assert len(parsed.text) <= 1024


async def test_a_corrupt_pdf_body_is_unreadable_not_a_crash():
    """前面有 %PDF- 头、后面是垃圾：magic bytes 过了，解析器必须给出判决。"""

    with pytest.raises(DocumentRejected) as exc:
        await parse_upload(b"%PDF-1.7\n" + b"\xff" * 4096, ".pdf")
    assert exc.value.code in {"document_unreadable", "no_indexable_text"}


async def test_parser_timeout_kills_the_subprocess(ingest):
    """超时的含义是「杀掉那个进程」，不是「不再等它」。

    0 秒超时让 wait_for 立刻放弃，随后走的是 `_kill_process_group`。这一条断言的是
    调用方拿到了 document_parse_timeout 而不是挂住 —— 一份 hang 住的解析器不能把
    整个请求拖到无限。
    """

    ingest.parse_timeout_seconds = 0.001
    with pytest.raises(DocumentRejected) as exc:
        await parse_upload(_pdf(1), ".pdf")
    assert exc.value.code == "document_parse_timeout"


async def test_plain_text_never_spawns_a_subprocess():
    parsed = await parse_upload("纯文本正文".encode("utf-8"), ".md")
    assert parsed.text == "纯文本正文"
    assert parsed.pages is None

"""上传文档的解析子进程。

**这个文件只许 import 标准库和解析器本身。** 它被 `document_parse.py` 以
``sys.executable <this file> <limits-json>`` 启动，不经过 `travel_agent` 包，所以
不会在每次解析时加载配置、建连接池或触发任何一次网络调用。上限由调用方以 JSON
传进来，因为一个只读参数比一个被 import 的全局配置更容易在崩溃后复现。

为什么是子进程而不是线程：pypdf / python-docx 处理的是**用户上传的不可信文件**，
一次解析可以在纯 C 代码里跑很久，而线程既杀不掉也限不住内存。子进程可以带
RLIMIT，超时可以杀整个进程组，进程退出时 OS 一并回收。

约定：正常退出码 0，stdout 一行 JSON。失败也是退出码 0 + 一行 JSON（带 ``code``），
被解析器自己搞死（OOM / CPU 上限 / segfault）才是非零退出码。
"""

from __future__ import annotations

import json
import sys
import zipfile
from typing import Any, Dict


def _fail(code: str, detail: str) -> Dict[str, Any]:
    return {"ok": False, "code": code, "detail": detail}


def _parse_pdf(path: str, limits: Dict[str, Any]) -> Dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    pages = len(reader.pages)
    max_pages = int(limits["max_pdf_pages"])
    if pages > max_pages:
        return _fail("document_too_complex", f"pdf has {pages} pages (max {max_pages})")
    max_chars = int(limits["max_extracted_chars"])
    collected: list[str] = []
    total = 0
    truncated = False
    for page in reader.pages:
        text = page.extract_text() or ""
        if not text.strip():
            continue
        # 逐页截断而不是拼完再切：一份恶意 PDF 的单页就能产出几百 MB 文本。
        remaining = max_chars - total
        if len(text) >= remaining:
            collected.append(text[:remaining])
            truncated = True
            break
        collected.append(text)
        total += len(text)
    return {
        "ok": True,
        "text": "\n\n".join(collected),
        "pages": pages,
        "truncated": truncated,
    }


def _parse_docx(path: str, limits: Dict[str, Any]) -> Dict[str, Any]:
    from docx import Document

    document = Document(path)
    max_chars = int(limits["max_extracted_chars"])
    collected: list[str] = []
    total = 0
    truncated = False
    for paragraph in document.paragraphs:
        text = paragraph.text
        if not text.strip():
            continue
        remaining = max_chars - total
        if len(text) >= remaining:
            collected.append(text[:remaining])
            truncated = True
            break
        collected.append(text)
        total += len(text)
    return {
        "ok": True,
        "text": "\n".join(collected),
        "pages": None,
        "truncated": truncated,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(json.dumps(_fail("worker_usage", "expected <suffix> <path> <limits-json>")))
        return 0
    suffix, path, raw_limits = argv[1], argv[2], argv[3]
    try:
        limits = json.loads(raw_limits)
    except ValueError as exc:
        print(json.dumps(_fail("worker_usage", f"bad limits json: {exc}")))
        return 0
    try:
        if suffix == ".pdf":
            result = _parse_pdf(path, limits)
        elif suffix == ".docx":
            result = _parse_docx(path, limits)
        else:
            result = _fail("unsupported_file_type", f"worker cannot parse {suffix}")
    except zipfile.BadZipFile as exc:
        result = _fail("document_unreadable", f"not a zip container: {exc}")
    except MemoryError:
        # RLIMIT_AS 命中时 Python 多半还能走到这里；走不到就是非零退出码，
        # 调用方按 document_unreadable 收口。
        result = _fail("document_too_complex", "parser exceeded its memory limit")
    except Exception as exc:  # 解析器的任何内部错误都是「这份文件打不开」
        result = _fail("document_unreadable", f"{type(exc).__name__}: {exc}")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

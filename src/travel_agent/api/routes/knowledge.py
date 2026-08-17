"""
知识库管理 API (Serving Layer)
支持文档上传、索引、查询和删除。

- Upload size cap
- 集合归属由服务端的本地身份解析，客户端不声明所有者
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from ...builders import get_components
from ...local_profile import LOCAL_USER_ID
from ...rag.collections import canonical_logical_collection, user_scoped_collection
from ...utils.user_text import content_length
from ..schemas import (
    KnowledgeCollectionStatsResponse,
    KnowledgeDeleteResponse,
    KnowledgeQueryRequest,
    KnowledgeQueryResponse,
    KnowledgeSourceDocumentResponse,
    KnowledgeSourceUpdateRequest,
    KnowledgeUploadRequest,
    KnowledgeUploadResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

_SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}
# Hard upload size limit (bytes).
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# 一份「进得去」的资料至少要有这么多非空白字符。**这个数只在这里写一次**：
# 它必须对四种格式一视同仁 —— 否则一个空 .md 上传成功、回执写着「已成功索引 0 个
# 文本块」，而「成功」与「0 个」在同一句话里。同一个入口对四种格式给两种答案，
# 说明那个数在错的层上。
_MIN_INDEXABLE_CHARS = 8

# 这条路上每一个 4xx 都带一个 `code`，**一个都不许少**。少一个的后果不是「少一句话」，
# 是**画成另一句话**：界面读不到 code 就按状态码回落，而 422 那一档的回落是
    # 「请求的信息不完整，请刷新页面后重试」—— 一份损坏的 PDF 刷一百次还是损坏的。
    # `message` 只给日志与其他客户端，**界面自己说话**，
# 那一侧的表在 `frontend/src/lib/knowledgeIngestFailure.ts`，两张表逐条对齐。
#
# `message` 里**不许**出现被 catch 到的异常原文：它是给这台机器看的（已经进
# `logger.warning`），发出去只会变成一句没人能照着做的话。

# 「太大了」在这条路上判两次（先看 content-length，再看真读进来多少），
# 而它是**同一种失败**，所以这份 detail 只写一次。
_TOO_LARGE_DETAIL = {
    "code": "file_too_large",
    "message": f"文件超过大小上限（{_MAX_UPLOAD_BYTES} 字节）",
}


def _logical_collection_name(collection: str) -> str:
    """Project the shared collection contract to an HTTP 400 boundary."""

    try:
        return canonical_logical_collection(collection)
    except ValueError as exc:
        logger.warning("资料库集合名不合法: %s", exc)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "collection_address_invalid",
                "message": "资料库地址不是一个合法的逻辑集合名",
            },
        ) from exc


def scope_collection(collection: str) -> str:
    """把逻辑集合名解析成本机用户的物理集合名。"""

    return user_scoped_collection(_logical_collection_name(collection))


def _require_indexable_text(text: str) -> str:
    """一份资料要么带着可索引的正文，要么这次上传就不算成功。

    四种格式与手输那一路共用这一道界（`_MIN_INDEXABLE_CHARS` 的注释写了为什么它在
    这一层）。扫描版 PDF 是这条路上最常见的一种：`pypdf` 不报错、逐页返回空串，
    没有这道界它就变成一次「成功索引 0 个文本块」。

    ``detail`` 走 ``{code, message}`` 那个形状（读取器在
    `frontend/src/lib/apiErrorDetail.ts`）：**话由界面说**，这里只负责说清是哪一种
    失败。少了这个 code，界面只能按状态码回落到「请求的信息不完整，请刷新页面后
    重试」——而请求是完整的、刷新也不会让这份文件长出正文。
    """

    # 「有几个字算有内容」的量法在 `utils/user_text.py`（偏好与记忆两处同一条）；
    # **阈值**是这个字段自己的属性，所以它留在这里。
    if content_length(text) < _MIN_INDEXABLE_CHARS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_indexable_text",
                "message": "没有可索引的正文（可能是空文件或扫描版 PDF）",
            },
        )
    return text


def _document_unreadable(kind: str, filename: str, error: Exception) -> HTTPException:
    """一份打不开的文档：说清是哪一种失败，**不转发解析器的原话**。

    界面那一侧按 code 分辨失败种类，读不到就只能按状态码回落到「请求的信息不完整，
    请刷新页面后重试」，而请求是完整的、刷新也不会让一份损坏的 PDF 变好。解析器那句
    原话（`Stream has ended unexpectedly` / `File is not a zip file`）留在日志里，
    那是它该待的地方。
    """

    logger.warning("%s 解析失败 [%s]: %s", kind, filename, error)
    return HTTPException(
        status_code=422,
        detail={
            "code": "document_unreadable",
            "message": "这份文档打不开，文件内容可能已损坏或不是它声称的格式",
        },
    )


def _extract_file_text(raw: bytes, suffix: str, filename: str) -> str:
    """从原始字节中提取纯文本，按后缀分派。

    这里只负责「取得正文」；「正文够不够」归 ``_require_indexable_text``，
    调用方在拿到结果之后统一问一次。**两种失败要分开**：打不开（这一函数，
    `document_unreadable`）与打开了但里面没字（那一函数，`no_indexable_text`）——
    前者换一份文件，后者把文件转成文字，不是同一件事。
    """
    if suffix in {".txt", ".md"}:
        return raw.decode("utf-8", errors="replace")

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n\n".join(p for p in pages if p.strip())
        except Exception as e:
            raise _document_unreadable("PDF", filename, e) from e

    if suffix == ".docx":
        try:
            from docx import Document

            doc = Document(io.BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            raise _document_unreadable("DOCX", filename, e) from e

    return raw.decode("utf-8", errors="replace")


@router.post("/index", response_model=KnowledgeUploadResponse)
async def index_text(request: KnowledgeUploadRequest):
    """将文本内容索引到知识库（用户作用域集合）。"""
    logical_collection = _logical_collection_name(request.collection)
    collection = scope_collection(logical_collection)
    # 手输那一路和上传文件走同一道界：空内容不许换来一句「成功索引 0 个文本块」。
    _require_indexable_text(request.content or "")
    components = get_components()
    try:
        count = await components.knowledge_indexer.index_text(
            text=request.content,
            source=request.source,
            collection=collection,
            metadata={**(request.metadata or {}), "owner_user_id": LOCAL_USER_ID},
        )
        return KnowledgeUploadResponse(
            chunks_indexed=count,
            collection=logical_collection,
            message=f"成功索引 {count} 个文本块",
        )
    except Exception as e:
        logger.error(f"知识库索引失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload-file", response_model=KnowledgeUploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    collection: str = Form("default"),
    source: str = Form(""),
):
    """上传文件并索引到知识库（支持 .txt / .md / .pdf / .docx；上限 10MB）。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_file_type",
                "message": "这个文件格式不在支持范围内",
            },
        )

    # Enforce size before reading whole body into memory when possible.
    # 413 必须带 code：界面那张按状态码说话的表没有 413 这一档时，一份超限文件
    # 只会换来「无法连接服务，请检查网络」。带上 code 之后它有自己的一句话。
    content_length = file.headers.get("content-length") if file.headers else None
    if content_length is not None:
        try:
            if int(content_length) > _MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)
        except ValueError:
            pass

    raw = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)

    extracted = _extract_file_text(raw, suffix, file.filename or "")
    text = _require_indexable_text(extracted)
    source_name = source or file.filename or "uploaded_file"
    logical_collection = _logical_collection_name(collection)
    scoped = scope_collection(logical_collection)

    components = get_components()
    try:
        count = await components.knowledge_indexer.index_text(
            text=text,
            source=source_name,
            collection=scoped,
            metadata={"owner_user_id": LOCAL_USER_ID, "filename": file.filename or ""},
        )
        return KnowledgeUploadResponse(
            chunks_indexed=count,
            collection=logical_collection,
            message=f"文件 [{file.filename}] 已成功索引 {count} 个文本块",
        )
    except Exception as e:
        logger.error(f"文件索引失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/query", response_model=KnowledgeQueryResponse)
async def query_knowledge(request: KnowledgeQueryRequest):
    """语义查询知识库（仅当前用户作用域集合）。"""
    collection = scope_collection(request.collection)
    components = get_components()
    try:
        docs = await components.vector_retriever.retrieve(
            query=request.query,
            collection=collection,
            top_k=request.top_k,
        )
        return KnowledgeQueryResponse(results=docs, total=len(docs))
    except Exception as e:
        logger.error(f"知识库查询失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/collection/{collection_name}", response_model=KnowledgeDeleteResponse)
async def delete_collection(collection_name: str):
    """删除指定集合。"""
    logical_collection = _logical_collection_name(collection_name)
    scoped = scope_collection(logical_collection)
    components = get_components()
    try:
        count = await components.knowledge_indexer.delete_collection(scoped)
        return KnowledgeDeleteResponse(
            collection=logical_collection,
            deleted_chunks=count,
            message=f"已删除资料库的 {count} 个文档块",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _require_source_document(collection: str, source: str) -> dict:
    """取一篇资料的正文，取不到就说清是哪一种取不到。

    两种「没有正文」必须分开回报，否则界面只能说一句含糊的话：

    - **这一篇不存在**（404 `unknown_source`）：来源名打错了，或它刚被删掉。
    - **这一篇有段、但正文没有留存**（409 `document_text_unavailable`）：
      `knowledge_documents` 之前入库的资料。这里**不许**把段拼回去当正文交出去 ——
      段带重叠、contextual 分块还带 LLM 前缀，拼出来的不是用户写下的东西，
      而一旦他按了保存，那份近似品就成了新的正文（见建表处的注释）。
    """

    indexer = get_components().knowledge_indexer
    document = await indexer.get_document(collection, source)
    if document is not None:
        return document
    if await indexer.count_source_chunks(collection, source) > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "document_text_unavailable",
                "message": "这篇资料入库时没有留存正文",
            },
        )
    raise HTTPException(
        status_code=404,
        detail={"code": "unknown_source", "message": "资料库里没有这一篇"},
    )


@router.get(
    "/collections/{collection_name}/source",
    response_model=KnowledgeSourceDocumentResponse,
)
async def get_source_document(
    collection_name: str,
    source: str = Query(..., min_length=1, description="来源名，就是列表里显示的那一行"),
):
    """读一篇资料的正文（用户作用域）。

    来源名走查询参数而不是路径段：它是用户给的文件名，可以带斜杠、点、空格 ——
    放进路径就要跟路由分段规则打架，而一个被路由切开的来源名会变成一次 404。
    """
    logical_collection = _logical_collection_name(collection_name)
    scoped = scope_collection(logical_collection)
    document = await _require_source_document(scoped, source)
    return KnowledgeSourceDocumentResponse(
        collection=logical_collection, **document
    )


@router.put(
    "/collections/{collection_name}/source",
    response_model=KnowledgeUploadResponse,
)
async def update_source_document(
    collection_name: str,
    request: KnowledgeSourceUpdateRequest,
    source: str = Query(..., min_length=1),
):
    """改写一篇资料的正文，并按新正文重新分段入库。

    **保存就是重新入库**：正文覆盖写、旧段全删、新段重算（会重新调 embedding，
    contextual 分块还会逐段调一次 fast 模型）。只改正文不重算段的话，屏幕上是新正文、
    规划时参考的仍是旧内容 —— 那是这个仓明令禁止的半修。

    先确认这一篇存在再写：这个接口是「改」不是「建」，让它顺手建一篇会让一个打错的
    来源名静默变成一篇新资料。
    """
    logical_collection = _logical_collection_name(collection_name)
    scoped = scope_collection(logical_collection)
    await _require_source_document(scoped, source)
    # 手输那一路、上传那一路、改写这一路共用同一道正文下界。
    content = _require_indexable_text(request.content or "")
    components = get_components()
    try:
        count = await components.knowledge_indexer.index_text(
            text=content,
            source=source,
            collection=scoped,
            metadata={"owner_user_id": LOCAL_USER_ID},
        )
        return KnowledgeUploadResponse(
            chunks_indexed=count,
            collection=logical_collection,
            message=f"已重新整理成 {count} 个文本块",
        )
    except Exception as e:
        logger.error(f"资料改写失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/collections/{collection_name}/source",
    response_model=KnowledgeDeleteResponse,
)
async def delete_source_document(
    collection_name: str,
    source: str = Query(..., min_length=1),
):
    """删除一篇资料（正文 + 它的全部段）。整库销毁仍走 `/collection/{name}`。"""
    logical_collection = _logical_collection_name(collection_name)
    scoped = scope_collection(logical_collection)
    components = get_components()
    result = await components.knowledge_indexer.delete_source(scoped, source)
    if not result["existed"]:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_source", "message": "资料库里没有这一篇"},
        )
    deleted = int(result["deleted_chunks"])
    return KnowledgeDeleteResponse(
        collection=logical_collection,
        deleted_chunks=deleted,
        message=f"已删除「{source}」，共 {deleted} 段资料",
    )


@router.get("/collections/{collection_name}/stats", response_model=KnowledgeCollectionStatsResponse)
async def get_collection_stats(collection_name: str):
    """获取集合统计信息（用户作用域）。"""
    logical_collection = _logical_collection_name(collection_name)
    scoped = scope_collection(logical_collection)
    components = get_components()
    try:
        stats = await components.knowledge_indexer.get_collection_stats(scoped)
        return KnowledgeCollectionStatsResponse(collection=logical_collection, **stats)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

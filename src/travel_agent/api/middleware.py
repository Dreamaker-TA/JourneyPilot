"""
FastAPI 中间件 (Serving Layer)
"""

from __future__ import annotations

import logging
import time
import uuid
from fastapi import Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)


async def request_logging_middleware(request: Request, call_next):
    """请求日志中间件：记录方法、路径、状态码与耗时。"""
    request_id = str(uuid.uuid4())[:8]
    start = time.time()

    response: Response = await call_next(request)

    elapsed = (time.time() - start) * 1000
    logger.info(
        f"[{request_id}] {request.method} {request.url.path} "
        f"→ {response.status_code} ({elapsed:.1f}ms)"
    )

    return response

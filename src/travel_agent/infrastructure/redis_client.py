"""
Redis 客户端封装 (Infrastructure Layer)
用于短期会话缓存、工具结果缓存等。
"""

from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis

from ..config import get_settings

logger = logging.getLogger(__name__)

_redis_client: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = aioredis.from_url(
            settings.redis.url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _redis_client


async def close_redis() -> None:
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None
    logger.info("Redis 连接已关闭")

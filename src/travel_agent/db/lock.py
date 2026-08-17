"""迁移互斥锁：PostgreSQL advisory lock。

即使是单用户本地应用也必须有这把锁（dev docs 02 §4.2）：用户重复敲 start、
Docker restart policy 与手工启动重叠、两个终端同时跑、上一次 migration 还没退出 ——
四种情况都会让两个 migrator 同时改同一个库。

**超时必须报错，不能降级成「那我不加锁跑」**：拿不到锁说明另一个 migrator 在跑，
此时继续等于两个进程并发 DDL。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# 锁名。`hashtext()` 在 PostgreSQL 里把它算成一个 int4，两个进程只要用同一个字符串
# 就落在同一把锁上。字符串本身进日志，便于用户在 pg_locks 里对上。
MIGRATION_LOCK_NAME = "journeypilot:migration"

DEFAULT_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.5


class MigrationLockTimeout(RuntimeError):
    """拿不到迁移锁。调用方必须退出，不允许无锁继续。"""


def _lock_key(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT hashtext(%s)", (MIGRATION_LOCK_NAME,))
        return int(cur.fetchone()[0])


@contextmanager
def migration_lock(
    conn: Any, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> Iterator[None]:
    """持有迁移锁的上下文。psycopg3 连接，必须是 autocommit。

    用 `pg_try_advisory_lock` 轮询而不是 `pg_advisory_lock` 阻塞等待：后者没有超时，
    一个卡死的 migrator 会让用户看到一个永远不返回、也不说话的命令。

    锁是 session 级的 —— 进程崩掉时 PostgreSQL 自动释放，所以不存在「上次崩了、
    锁留在库里、从此谁也迁不了」这种需要人工清理的状态。
    """

    key = _lock_key(conn)
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    waited = False

    while True:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = bool(cur.fetchone()[0])
        if acquired:
            break
        if time.monotonic() >= deadline:
            raise MigrationLockTimeout(
                f"等待迁移锁 {MIGRATION_LOCK_NAME} 超过 {timeout_seconds:.0f}s。"
                "另一个 journeypilot migrate / 启动编排器正在改这个数据库；"
                "确认它已退出后重试，不要并发迁移。"
            )
        if not waited:
            logger.info("迁移锁被占用，等待中（最长 %.0fs）…", timeout_seconds)
            waited = True
        time.sleep(_POLL_INTERVAL_SECONDS)

    try:
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (key,))


def try_acquire(conn: Any) -> bool:
    """单次尝试，给测试和 doctor 用（doctor 只想知道「现在有人在迁移吗」）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_lock_key(conn),))
        return bool(cur.fetchone()[0])


def release(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(conn),))

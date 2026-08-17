"""恢复：先恢复到一个新库，验证通过再切换。

dev docs 02 §6 那句禁令是这个模块存在的全部理由：**「不要直接覆盖当前 volume 后
祈祷成功。」** 直接 `pg_restore` 进当前库有两个不可接受的后果 —— 恢复中途失败时
当前库已经被改坏了，而且没有任何东西可以回退。

所以流程是（§6）：

```
校验 manifest / checksum
→ 检查兼容范围（备份格式版本、PostgreSQL major）
→ 建一个 staging 库
→ pg_restore 进 staging
→ 只读结构校验（指纹 == 备份里的指纹）
→ 数据完整性校验（行数 == manifest 的行数）
→ 切换：当前库改名留档，staging 改名上位
→ 旧库保留，等用户确认后再删
```

切换是重命名而不是删除：`<db>_prerestore_<时间戳>` 一直留在服务器上，直到用户
显式敲 `journeypilot restore --drop-retained <名字>`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backup import DUMP_FILENAME, FINGERPRINT_FILENAME, load_manifest, verify_backup
from .census import take_census
from .connection import DatabaseTarget, connect, connect_maintenance
from .fingerprint import diff_fingerprints, fingerprint_sync
from .pg_tools import resolve_runner

logger = logging.getLogger(__name__)


class RestoreError(RuntimeError):
    """恢复失败。**当前数据库未被修改**（切换是最后一步，且是重命名）。"""


@dataclass
class RestoreResult:
    restored_database: str
    retained_database: str
    manifest: dict[str, Any]
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "restored_database": self.restored_database,
            "retained_database": self.retained_database,
            "backup_created_at": self.manifest.get("created_at"),
            "migration_revision": self.manifest.get("migration_revision"),
            "checks": self.checks,
        }


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _quote(identifier: str) -> str:
    """库名进 SQL。`CREATE DATABASE` 不能参数化，所以只能引号 + 断言。"""
    if '"' in identifier or not identifier:
        raise RestoreError(f"非法数据库名：{identifier!r}")
    return f'"{identifier}"'


def _database_exists(conn: Any, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        return cur.fetchone() is not None


def _active_connections(conn: Any, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        return int(cur.fetchone()[0])


def _terminate_connections(conn: Any, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        return int(cur.fetchone()[0])


def preflight(directory: Path, target: DatabaseTarget, *, server_major: int) -> dict[str, Any]:
    """在碰任何数据库之前，把备份和环境都验一遍。返回 manifest。"""

    problems = verify_backup(directory)
    if problems:
        raise RestoreError(
            "备份校验未通过，拒绝恢复：\n  - " + "\n  - ".join(problems)
        )

    manifest = load_manifest(directory)
    source_major = int(manifest.get("database", {}).get("postgres_major") or 0)
    if source_major > server_major:
        raise RestoreError(
            f"备份来自 PostgreSQL {source_major}，当前服务端是 {server_major}。"
            "不支持向下恢复到更旧的 major 版本；请先升级服务端。"
        )
    if source_major and source_major < server_major:
        logger.warning(
            "跨 PostgreSQL major 恢复：备份来自 %s，目标是 %s。"
            "使用 logical dump 恢复（不复制原始 volume），"
            "扩展与可选能力（zhparser）需要在目标端各自可用。",
            source_major, server_major,
        )
    return manifest


def restore(
    target: DatabaseTarget,
    directory: Path,
    *,
    embedding_dimensions: int,
    terminate_active: bool = False,
    preferred_container: str = "",
) -> RestoreResult:
    """把 `directory` 里的备份恢复成 `target.database`，旧库改名留档。"""

    directory = directory.resolve()

    with connect(target) as conn:
        census = take_census(conn)
    server_major = census.postgres_major

    manifest = preflight(directory, target, server_major=server_major)
    runner = resolve_runner(
        target, server_major=server_major, preferred_container=preferred_container
    )

    stamp = _stamp()
    staging_name = f"{target.database}_restore_{stamp}"
    retained_name = f"{target.database}_prerestore_{stamp}"
    staging_target = target.with_database(staging_name)

    with connect_maintenance(target) as maintenance:
        if _database_exists(maintenance, staging_name):  # pragma: no cover — 时间戳撞车
            raise RestoreError(f"staging 库已存在：{staging_name}")
        with maintenance.cursor() as cur:
            cur.execute(f"CREATE DATABASE {_quote(staging_name)}")
    logger.info("已创建 staging 库 %s", staging_name)

    try:
        result = runner.run(
            "pg_restore",
            [
                "--dbname", staging_name,
                "--no-owner", "--no-acl",
                # 不用 --clean/--create：staging 是新建的空库，那两个开关只会
                # 带来「它到底在删哪个库」的歧义。
                "--exit-on-error",
            ],
            target=target,
            stdin_path=directory / DUMP_FILENAME,
        )
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise RestoreError(f"pg_restore 失败（exit {result.returncode}）：\n{stderr}")

        checks = _verify_restored(
            staging_target, directory, manifest, embedding_dimensions=embedding_dimensions
        )
    except Exception:
        # staging 是我们自己建的，失败就收掉；当前库全程未被触碰。
        with connect_maintenance(target) as maintenance:
            with maintenance.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS {_quote(staging_name)}")
        logger.info("恢复失败，已清理 staging 库 %s；当前数据库未被修改", staging_name)
        raise

    # ---- 切换 ------------------------------------------------------------- #
    with connect_maintenance(target) as maintenance:
        active = _active_connections(maintenance, target.database)
        if active:
            if not terminate_active:
                with maintenance.cursor() as cur:
                    cur.execute(f"DROP DATABASE IF EXISTS {_quote(staging_name)}")
                raise RestoreError(
                    f"当前数据库 {target.database} 上还有 {active} 个活动连接。"
                    "先停掉 API / 开发进程再恢复，"
                    "或用 --terminate-active 强制断开它们。"
                    "（已清理 staging 库，当前数据库未被修改。）"
                )
            killed = _terminate_connections(maintenance, target.database)
            logger.warning("已强制断开 %d 个到 %s 的连接", killed, target.database)

        with maintenance.cursor() as cur:
            cur.execute(
                f"ALTER DATABASE {_quote(target.database)} RENAME TO {_quote(retained_name)}"
            )
            cur.execute(
                f"ALTER DATABASE {_quote(staging_name)} RENAME TO {_quote(target.database)}"
            )

    logger.info(
        "恢复完成：%s 已就位；恢复前的数据库保留为 %s（确认无误后可用 "
        "`journeypilot restore --drop-retained %s` 删除）",
        target.database, retained_name, retained_name,
    )
    return RestoreResult(
        restored_database=target.database,
        retained_database=retained_name,
        manifest=manifest,
        checks=checks,
    )


def _verify_restored(
    staging: DatabaseTarget,
    directory: Path,
    manifest: dict[str, Any],
    *,
    embedding_dimensions: int,
) -> dict[str, Any]:
    """恢复到 staging 之后的只读校验（§6.1）。任何一项不过就不切换。"""

    import json

    checks: dict[str, Any] = {}
    with connect(staging) as conn:
        actual_fingerprint = fingerprint_sync(conn, embedding_dimensions=embedding_dimensions)
        census = take_census(conn)

    expected_path = directory / FINGERPRINT_FILENAME
    if expected_path.exists():
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        problems = diff_fingerprints(expected, actual_fingerprint)
        checks["schema_matches_backup"] = not problems
        if problems:
            raise RestoreError(
                "恢复出来的结构与备份记录的结构不一致，拒绝切换：\n  - "
                + "\n  - ".join(problems)
            )
    else:  # pragma: no cover — 只有手工拼的备份目录会缺它
        checks["schema_matches_backup"] = None

    expected_counts: dict[str, int] = manifest.get("row_counts") or {}
    mismatches = [
        f"{table}：manifest {expected_counts[table]}，恢复后 {census.row_counts.get(table)}"
        for table in sorted(expected_counts)
        if census.row_counts.get(table) != expected_counts[table]
    ]
    checks["row_counts_match"] = not mismatches
    if mismatches:
        raise RestoreError(
            "恢复后的行数与 manifest 不一致，拒绝切换：\n  - " + "\n  - ".join(mismatches)
        )

    expected_revision = manifest.get("migration_revision")
    checks["migration_revision"] = census.alembic_revision
    if expected_revision != census.alembic_revision:
        raise RestoreError(
            f"恢复后的 migration revision 是 {census.alembic_revision}，"
            f"备份记录的是 {expected_revision}，拒绝切换。"
        )

    checks["total_business_rows"] = census.total_business_rows
    return checks


def drop_retained(target: DatabaseTarget, name: str) -> None:
    """删掉恢复时留档的旧库。**只允许删 `_prerestore_` 后缀的那些。**

    这条限制不是洁癖：这个函数拿到的是一个从命令行来的库名，而它执行的是
    `DROP DATABASE`。限制在我们自己造的名字上，意味着敲错一个字母删不掉真实的库。
    """

    if f"{target.database}_prerestore_" not in name:
        raise RestoreError(
            f"只允许删除恢复留档库（`{target.database}_prerestore_*`），拒绝删除 {name!r}"
        )
    with connect_maintenance(target) as maintenance:
        with maintenance.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_quote(name)}")
    logger.info("已删除恢复留档库 %s", name)


def list_retained(target: DatabaseTarget) -> list[str]:
    with connect_maintenance(target) as maintenance:
        with maintenance.cursor() as cur:
            cur.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s ORDER BY datname DESC",
                (f"{target.database}_prerestore_%",),
            )
            return [row[0] for row in cur.fetchall()]

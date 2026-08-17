"""API 进程唯一被允许调用的数据库生命周期模块：只读合同校验。

只跑 SELECT，回答「版本号是什么、结构对不对、缺什么」。结论进启动日志，并决定
`GET /api/health/ready` 放不放行。真正「不启动」的闸门在编排器一侧
（`journeypilot migrate` 不通过就不 exec API）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .fingerprint import diff_fingerprints, fingerprint_async, fingerprint_digest
from .schema_contract import EXTERNALLY_OWNED_TABLES, MANAGED_TABLES

logger = logging.getLogger(__name__)

#: 合同不通过时 readiness 返回 503。判据与 `api/routes/system.py` 的非阻塞名单同源。
GATES_READINESS = True


class DatabaseContractError(RuntimeError):
    """数据库结构不满足当前代码的合同。消息里带上 `next_action`。"""


@dataclass
class SchemaReport:
    """一次只读校验的结果。字段直接进 readiness JSON，所以是对外合同。"""

    reachable: bool
    revision: str | None = None
    head_revision: str = ""
    #: 结构与「revision 应有的样子」是否一致
    schema_matches_revision: bool | None = None
    fingerprint_sha256: str = ""
    missing_tables: tuple[str, ...] = ()
    #: public schema 里既不属于合同、也没有已知外部 owner 的表
    unmanaged_tables: tuple[str, ...] = ()
    optional_capabilities: dict[str, Any] = field(default_factory=dict)
    embedding_columns: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    next_action: str = ""

    @property
    def managed(self) -> bool:
        """这个库是否已被版本化迁移纳管（有 revision）。"""
        return bool(self.revision)

    @property
    def compatible(self) -> bool:
        """当前代码能不能安全地读写这个库。判据就是 `problems` 空不空。"""
        return self.reachable and not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "managed": self.managed,
            "revision": self.revision,
            "head_revision": self.head_revision,
            "schema_matches_revision": self.schema_matches_revision,
            "compatible": self.compatible,
            "gates_readiness": GATES_READINESS,
            "fingerprint_sha256": self.fingerprint_sha256,
            "missing_tables": list(self.missing_tables),
            "unmanaged_tables": list(self.unmanaged_tables),
            "optional_capabilities": self.optional_capabilities,
            "embedding_columns": self.embedding_columns,
            "problems": list(self.problems),
            "next_action": self.next_action,
        }

    def log_summary(self) -> None:
        """把结论写成运营者能直接照做的一行（或几行）。"""

        if not self.compatible:
            logger.error(
                "数据库合同校验未通过，服务不会进入就绪：%s | 下一步：%s",
                "；".join(self.problems) or "库不可达",
                self.next_action or "journeypilot doctor",
            )
        else:
            logger.info(
                "数据库合同校验通过：revision %s（head %s），指纹 %s…",
                self.revision, self.head_revision, self.fingerprint_sha256[:12],
            )

        disabled = [name for name, on in self.optional_capabilities.items() if on is False]
        if disabled:
            logger.warning(
                "可选能力未启用：%s。中文词法检索使用 '%s' 分词，能力是降级的 —— "
                "不要按「完整中文分词」理解检索结果。",
                "、".join(disabled),
                self.optional_capabilities.get("text_search_config") or "simple",
            )


_ALEMBIC_REVISION_SQL = """
    SELECT version_num FROM alembic_version
"""

_PUBLIC_TABLES_SQL = """
    SELECT c.relname AS name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
    ORDER BY c.relname
"""


async def verify_database_contract(engine: Any, *, embedding_dimensions: int) -> SchemaReport:
    """校验这个库是否满足当前代码的合同。只读，且永不抛异常。

    永不抛：调用点是 FastAPI lifespan，起不来就读不到 readiness 诊断。
    """

    from sqlalchemy import text

    from .migrate import expected_fingerprint_for, revision_line

    try:
        async with engine.connect() as conn:
            fingerprint = await fingerprint_async(
                conn, embedding_dimensions=embedding_dimensions
            )

            result = await conn.execute(text(_PUBLIC_TABLES_SQL))
            all_tables = [row["name"] for row in result.mappings()]

            revision: str | None = None
            if "alembic_version" in all_tables:
                result = await conn.execute(text(_ALEMBIC_REVISION_SQL))
                row = result.first()
                revision = row[0] if row else None
    except Exception as exc:
        return SchemaReport(
            reachable=False,
            problems=[f"{type(exc).__name__}: {exc}"],
            next_action="journeypilot doctor  # 先确认数据库可达",
        )

    try:
        line = revision_line()
    except Exception as exc:
        # 读不到迁移历史就说不出代码的 head 是哪一个，也就无从判断结构对不对。
        return SchemaReport(
            reachable=True,
            fingerprint_sha256=fingerprint_digest(fingerprint),
            problems=[f"读不到迁移历史：{type(exc).__name__}: {exc}"],
            next_action="journeypilot doctor --json",
        )

    unmanaged = tuple(
        name
        for name in all_tables
        if name not in MANAGED_TABLES and name not in EXTERNALLY_OWNED_TABLES
    )
    missing = tuple(fingerprint.get("missing_tables", ()))
    head = line[-1] if line else ""

    problems: list[str] = []
    schema_matches: bool | None = None
    # `next_action` 按问题类型决定，不按分支顺序覆盖：未纳管/版本落后 migrate 能修，
    # 版本已在 head 但结构漂移则不能（migrate 无事可做），只能导诊断后恢复或重建。
    next_action = ""

    if missing:
        problems.append(f"缺表：{'、'.join(missing)}")

    if revision is None:
        problems.append(
            "这个数据库还没有被版本化迁移纳管（没有 alembic_version），"
            "无法确认它的结构与当前代码匹配"
        )
        next_action = "journeypilot migrate  # 比对指纹后纳管，不会重复建表"
    else:
        expected = expected_fingerprint_for(revision, line=line)
        if expected is None:
            problems.append(f"revision `{revision}` 没有对应的指纹存档，无法校验结构")
            next_action = "journeypilot doctor --json"
        else:
            differences = diff_fingerprints(expected, fingerprint)
            schema_matches = not differences
            if differences:
                problems.extend(differences[:10])
                if len(differences) > 10:
                    problems.append(f"（另有 {len(differences) - 10} 处差异，见 doctor --json）")

        if revision != head:
            problems.append(f"数据库在 revision `{revision}`，代码的 head 是 `{head}`")
            next_action = "journeypilot migrate"
        elif schema_matches is False:
            next_action = "journeypilot doctor --json  # 版本号已在 head，结构却与合同不符"

    if unmanaged:
        # 不算 problem：多一张没人管的表不影响这份代码读写自己的表。
        logger.info(
            "public schema 里有 %d 张合同外的表（既不受迁移管辖，也没有已知外部 owner）：%s",
            len(unmanaged), "、".join(unmanaged),
        )

    return SchemaReport(
        reachable=True,
        revision=revision,
        head_revision=head,
        schema_matches_revision=schema_matches,
        fingerprint_sha256=fingerprint_digest(fingerprint),
        missing_tables=missing,
        unmanaged_tables=unmanaged,
        optional_capabilities=fingerprint.get("optional_capabilities", {}),
        embedding_columns=fingerprint.get("embedding_columns", {}),
        problems=problems,
        next_action=next_action,
    )


async def require_database_contract() -> SchemaReport:
    """同一份校验的「不通过就抛」版本，给没有 readiness 探针的一次性脚本用。"""

    from ..config import get_settings
    from ..infrastructure.database import get_engine

    settings = get_settings()
    report = await verify_database_contract(
        get_engine(), embedding_dimensions=settings.embedding.dimensions
    )
    if not report.compatible:
        raise DatabaseContractError(
            "数据库结构不满足当前代码的合同："
            + "；".join(report.problems)
            + f"。下一步：{report.next_action or 'journeypilot doctor'}"
        )
    return report

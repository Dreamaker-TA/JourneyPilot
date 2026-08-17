"""API 进程唯一被允许调用的数据库生命周期模块：**只读** schema 报告。

ADR-P0-03 把「改 Schema」整体移出 API 进程。P0-A 这一阶段还不删 `init_db()`
（那是 PR-P0-02 的事），但先把「API 该怎么看数据库」这件事建立起来：

- 只跑 `SELECT`，一条 DDL 都没有；
- 只回答三个问题：**版本号是什么、结构对不对、缺什么**；
- 结论进日志和 `GET /api/health/ready`，让运营者一眼看到，而不是等第一条请求 500。

## 这一阶段刻意**不**拦启动

`gates_readiness` 现在恒为 False。理由：`init_db()` 还在跑，它会把结构建成合同的样子
但**不写 `alembic_version`** —— 于是每一个现有部署在纳管之前都处于「结构对、版本号空」
的状态。这个阶段就让它拦启动，等于用一条诊断信息换掉所有人的可用性，而问题本身
（升级期数据安全）还没有被这一个 PR 解决。

所以现在的合同是：**说得非常清楚，但不拦。** 拦的那一步在 PR-P0-02 —— 那时
`init_db()` 变成 `verify_database_contract()`，迁移由启动编排器执行，"版本号是空的"
才真正意味着"这个库没有被任何迁移管过"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .fingerprint import diff_fingerprints, fingerprint_async, fingerprint_digest
from .schema_contract import EXTERNALLY_OWNED_TABLES, MANAGED_TABLES

logger = logging.getLogger(__name__)

#: 这一阶段报告不参与 readiness 门禁（理由见模块 docstring）。
GATES_READINESS = False


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
        """当前代码能不能安全地读写这个库。"""
        return self.reachable and not self.missing_tables and self.schema_matches_revision is not False

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

        if not self.reachable:
            logger.error("数据库 schema 报告：库不可达（%s）", "；".join(self.problems))
            return

        if self.problems:
            logger.error(
                "数据库 schema 报告：%s | 下一步：%s",
                "；".join(self.problems),
                self.next_action or "journeypilot doctor",
            )
        else:
            logger.info(
                "数据库 schema 报告：revision %s（head %s），结构与合同一致，指纹 %s…",
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

_UNMANAGED_TABLES_SQL = """
    SELECT c.relname AS name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
    ORDER BY c.relname
"""


async def build_schema_report(engine: Any, *, embedding_dimensions: int) -> SchemaReport:
    """从 SQLAlchemy 异步引擎读一份只读报告。绝不修改任何东西。"""

    from sqlalchemy import text

    from .migrate import expected_fingerprint_for, revision_line

    try:
        async with engine.connect() as conn:
            fingerprint = await fingerprint_async(
                conn, embedding_dimensions=embedding_dimensions
            )

            result = await conn.execute(text(_UNMANAGED_TABLES_SQL))
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

    unmanaged = tuple(
        name
        for name in all_tables
        if name not in MANAGED_TABLES and name not in EXTERNALLY_OWNED_TABLES
    )
    missing = tuple(fingerprint.get("missing_tables", ()))

    line = revision_line()
    head = line[-1] if line else ""

    problems: list[str] = []
    schema_matches: bool | None = None
    # `next_action` 必须是一条**真能修好当前问题**的命令。所以它按「问题是什么」
    # 决定，而不是按代码里几个分支的先后顺序覆盖：
    #   未纳管 / 版本落后        → migrate 能修
    #   版本已在 head 但结构不符 → migrate 什么都不会做（它会判 UP_TO_DATE 之外的拒绝），
    #                             真正的下一步是导诊断、再从备份恢复或重建
    next_action = ""

    if missing:
        problems.append(f"缺表：{'、'.join(missing)}")

    if revision is None:
        problems.append(
            "这个数据库还没有被版本化迁移纳管（没有 alembic_version）。"
            "当前由 init_db() 建表，升级前没有自动备份"
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
        # 但要留下痕迹 —— 它通常是上一个版本留下的遗留表。
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

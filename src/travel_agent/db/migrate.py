"""迁移决策与执行。

**这个模块是「启动应用」和「永久改写用户数据」之间的那道闸门**（dev docs 02 §1）。
它的核心不是「跑 alembic upgrade」——那一行谁都会写 —— 而是 §4.1 那个状态机里
**什么时候不许自动跑**：

- 来历不明的 schema：拒绝，绝不 `alembic stamp head`（§2.3）；
- 非空库：先备份且校验通过，才允许自动迁移（§4.3）；
- 标记为破坏性的迁移：必须显式 `--allow-destructive`；
- 数据库版本比代码新：拒绝启动，不假装能读。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .census import DatabaseCensus, take_census
from .connection import DatabaseTarget
from .fingerprint import diff_fingerprints, fingerprint_digest, fingerprint_sync
from .schema_contract import BASELINE_REVISION

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
MIGRATIONS_DIR = _REPO_ROOT / "migrations"
#: 每个**改变了受管结构**的 revision 在这里存一份指纹。adoption 判定与 API 只读校验
#: 都拿活库指纹与这里的存档逐字比对。不改结构的 revision（比如只删合同外的遗留表）
#: 不需要新存档 —— 指纹只覆盖 `MANAGED_TABLES`，它们本来就不在里面。
FINGERPRINTS_DIR = MIGRATIONS_DIR / "fingerprints"
BASELINE_FINGERPRINT_PATH = FINGERPRINTS_DIR / f"{BASELINE_REVISION}.json"


class Decision(str, Enum):
    """状态机的出口。名字直接进 doctor 的 JSON，所以是合同的一部分。"""

    #: 空库 → 直接 upgrade head，不需要备份
    MIGRATE_EMPTY = "migrate_empty"
    #: 有核心表、无 alembic_version、指纹等于 baseline → stamp baseline
    ADOPT_BASELINE = "adopt_baseline"
    #: 已在 head → 只做只读校验
    UP_TO_DATE = "up_to_date"
    #: 落后于 head → 备份后 upgrade
    UPGRADE = "upgrade"
    #: 有核心表、无 alembic_version、指纹与 baseline 不符 → 拒绝
    REFUSE_UNKNOWN_SCHEMA = "refuse_unknown_schema"
    #: alembic_version 里的 revision 这份代码不认识 → 拒绝
    REFUSE_UNKNOWN_REVISION = "refuse_unknown_revision"
    #: 数据库比代码新 → 拒绝（不支持 downgrade 到旧代码）
    REFUSE_SCHEMA_AHEAD = "refuse_schema_ahead"
    #: 待执行的迁移里有破坏性的，但没有授权 → 拒绝
    REFUSE_NEEDS_DESTRUCTIVE_CONSENT = "refuse_needs_destructive_consent"
    #: 连不上库
    REFUSE_UNREACHABLE = "refuse_unreachable"


_REFUSALS = {
    Decision.REFUSE_UNKNOWN_SCHEMA,
    Decision.REFUSE_UNKNOWN_REVISION,
    Decision.REFUSE_SCHEMA_AHEAD,
    Decision.REFUSE_NEEDS_DESTRUCTIVE_CONSENT,
    Decision.REFUSE_UNREACHABLE,
}


@dataclass
class MigrationPlan:
    decision: Decision
    census: DatabaseCensus
    head_revision: str
    current_revision: str | None
    pending_revisions: tuple[str, ...] = ()
    destructive_revisions: tuple[str, ...] = ()
    #: 拒绝或需要人工介入时，说清「差在哪」
    problems: tuple[str, ...] = ()
    #: 下一条可执行的命令（doctor 合同要求，dev docs 02 §12）
    next_action: str = ""
    fingerprint_digest: str = ""
    #: 活库的完整指纹。doctor 要用它报告实际向量维度与可选能力，
    #: 而重新读一遍库就意味着「同一份事实两个来源」。
    fingerprint: dict[str, Any] = field(default_factory=dict)
    #: 非空库执行迁移前必须先备份
    backup_required: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return self.decision in _REFUSALS

    @property
    def requires_migration(self) -> bool:
        return self.decision in {
            Decision.MIGRATE_EMPTY,
            Decision.ADOPT_BASELINE,
            Decision.UPGRADE,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "refused": self.refused,
            "head_revision": self.head_revision,
            "current_revision": self.current_revision,
            "pending_revisions": list(self.pending_revisions),
            "destructive_revisions": list(self.destructive_revisions),
            "backup_required": self.backup_required,
            "schema_fingerprint": self.fingerprint_digest,
            "problems": list(self.problems),
            "next_action": self.next_action,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Alembic 接线
# --------------------------------------------------------------------------- #


def alembic_config(target: DatabaseTarget | None = None, *, connection: Any = None) -> Any:
    """构造 Alembic Config。URL 只从 `target` 来（见 alembic.ini 的注释）。

    `target=None` 只用于**读迁移脚本目录**（revision 序列、destructive 标记）——
    那是纯文件操作，不连库，所以 API 进程也可以调用它。
    """

    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if target is not None:
        config.attributes["db_url"] = target.sqlalchemy_url
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def _script_directory(target: DatabaseTarget | None = None) -> Any:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config(target))


def revision_line(target: DatabaseTarget | None = None) -> tuple[str, ...]:
    """从 base 到 head 的 revision 序列。

    刻意只支持**线性**历史：本地单用户产品的升级是一条直线，分支合并带来的
    「两条路都对但结果不同」在这里没有价值，只有额外的失败模式。发现分叉就报错，
    而不是选一条继续。
    """

    scripts = _script_directory(target)
    line: list[str] = []
    for script in scripts.walk_revisions():  # head → base
        if isinstance(script.down_revision, (tuple, list)) and len(script.down_revision) > 1:
            raise RuntimeError(
                f"迁移历史出现分支合并（{script.revision}），"
                "JourneyPilot 只支持线性迁移历史；请把它改写成一条直线。"
            )
        line.append(script.revision)
    return tuple(reversed(line))


def revision_module(target: DatabaseTarget | None, revision: str) -> Any:
    return _script_directory(target).get_revision(revision).module


def is_destructive(target: DatabaseTarget | None, revision: str) -> bool:
    """迁移自己声明的 `destructive` 标记。缺声明按 False 处理。

    **不猜**：不去解析 SQL 里有没有 DROP。声明是迁移作者的责任，
    而一个会漏判的猜测比一个明确的约定更危险。
    """

    return bool(getattr(revision_module(target, revision), "destructive", False))


# --------------------------------------------------------------------------- #
# 决策
# --------------------------------------------------------------------------- #


def load_fingerprint_archive(revision: str) -> dict[str, Any] | None:
    """某个 revision 的指纹存档。没有存档返回 None。"""

    import json

    path = FINGERPRINTS_DIR / f"{revision}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_baseline_fingerprint() -> dict[str, Any] | None:
    return load_fingerprint_archive(BASELINE_REVISION)


def expected_fingerprint_for(revision: str, *, line: tuple[str, ...] | None = None) -> dict[str, Any] | None:
    """这个 revision 应该长什么样：从它往前找到最近的一份指纹存档。

    往前找而不是要求每个 revision 都有存档：只删合同外遗留表这种迁移不改变受管结构，
    给它复制一份一模一样的指纹只会制造「两份文件必须同步」的维护负担。
    """

    line = line if line is not None else revision_line()
    if revision not in line:
        return None
    for candidate in reversed(line[: line.index(revision) + 1]):
        archive = load_fingerprint_archive(candidate)
        if archive is not None:
            return archive
    return None


def plan(
    conn: Any,
    target: DatabaseTarget,
    *,
    embedding_dimensions: int,
    allow_destructive: bool = False,
) -> MigrationPlan:
    """看一眼库，决定接下来能做什么。**只读**，不改任何东西。"""

    census = take_census(conn)
    line = revision_line(target)
    head = line[-1] if line else ""
    current = census.alembic_revision

    actual = fingerprint_sync(conn, embedding_dimensions=embedding_dimensions)
    digest = fingerprint_digest(actual)

    def build(decision: Decision, **kwargs: Any) -> MigrationPlan:
        return MigrationPlan(
            decision=decision,
            census=census,
            head_revision=head,
            current_revision=current,
            fingerprint_digest=digest,
            fingerprint=actual,
            **kwargs,
        )

    # ---- 有 alembic_version：按 revision 判断 ---------------------------- #
    if census.has_alembic_version and current:
        if current not in line:
            return build(
                Decision.REFUSE_UNKNOWN_REVISION,
                problems=(
                    f"数据库记录的 revision `{current}` 不在这份代码的迁移历史里。"
                    "通常意味着代码被回滚到了比数据库更早的版本。",
                ),
                next_action=(
                    "升级到包含该 revision 的代码版本，"
                    "或用 `journeypilot restore <备份目录>` 回到与代码匹配的快照。"
                ),
            )
        pending = line[line.index(current) + 1 :]
        if not pending:
            # 「最新 → verify」（dev docs 02 §4.1）：版本号说 head 不等于结构真的是 head。
            # 少了一张表的库如果被判成 UP_TO_DATE，`migrate` 会什么都不做，而
            # 第一条写那张表的请求 500 —— 这正是版本号存在要消灭的那种沉默。
            expected = expected_fingerprint_for(current, line=line)
            drift = tuple(diff_fingerprints(expected, actual)) if expected else ()
            if drift:
                return build(
                    Decision.REFUSE_UNKNOWN_SCHEMA,
                    problems=(
                        f"数据库记录的 revision 是 `{current}`（已是 head），"
                        "但实际结构与该 revision 的合同不一致：",
                        *drift,
                    ),
                    next_action=(
                        "先 `journeypilot backup` 留档，再从匹配的备份恢复或重建数据库。"
                        "结构漂移不能靠再跑一次 migrate 修好 —— 版本号已经在 head 了。"
                    ),
                )
            return build(Decision.UP_TO_DATE, next_action="")

        destructive = tuple(rev for rev in pending if is_destructive(target, rev))
        if destructive and not allow_destructive:
            return build(
                Decision.REFUSE_NEEDS_DESTRUCTIVE_CONSENT,
                pending_revisions=pending,
                destructive_revisions=destructive,
                backup_required=census.total_business_rows > 0,
                problems=(
                    "待执行的迁移里有会丢数据或不可逆的：" + "、".join(destructive),
                ),
                next_action="journeypilot migrate --allow-destructive",
            )
        return build(
            Decision.UPGRADE,
            pending_revisions=pending,
            destructive_revisions=destructive,
            backup_required=census.total_business_rows > 0,
            next_action="journeypilot migrate",
        )

    # ---- 没有 alembic_version --------------------------------------------- #
    if census.is_empty_database:
        # 空库**不受破坏性闸门约束**：那道闸门保护的是已有数据，而这里没有数据可丢。
        # 首次安装因此不需要 `--allow-destructive`，即使 head 路径上有破坏性迁移
        # （它们在空库上都是空操作）。
        return build(
            Decision.MIGRATE_EMPTY,
            pending_revisions=line,
            destructive_revisions=(),
            backup_required=False,
            next_action="journeypilot migrate",
        )

    if not census.has_core_tables:
        # 有合同表但没有一张探针表：这不是 JourneyPilot 的库，也不是空库。
        return build(
            Decision.REFUSE_UNKNOWN_SCHEMA,
            problems=(
                "这个数据库里有部分 JourneyPilot 表，但缺少核心表 "
                f"({', '.join(census.missing_managed_tables)})，无法判定它的来历。",
            ),
            next_action="journeypilot doctor --json  # 导出诊断后决定重建或恢复备份",
        )

    # 有核心表、无版本号 → adoption：**必须先证明结构等于 baseline**。
    baseline = load_baseline_fingerprint()
    if baseline is None:
        return build(
            Decision.REFUSE_UNKNOWN_SCHEMA,
            problems=(
                f"缺少 baseline 指纹存档（{BASELINE_FINGERPRINT_PATH}），"
                "无法证明现有结构等于 baseline。",
            ),
            next_action="journeypilot db write-baseline-fingerprint  # 仅开发者，用于生成存档",
        )

    problems = tuple(diff_fingerprints(baseline, actual))
    if problems:
        return build(
            Decision.REFUSE_UNKNOWN_SCHEMA,
            problems=problems,
            next_action=(
                "这个数据库的结构与 baseline 不一致，拒绝自动纳管（绝不 stamp 未知结构）。"
                "先 `journeypilot backup` 留档，再重建数据库或从匹配的备份恢复。"
            ),
        )

    # 纳管本身（写一行 `alembic_version`）不改变任何数据，所以它**不受破坏性闸门约束**。
    # 闸门管的是纳管之后的那些待执行迁移 —— `cmd_migrate` 先 stamp、再重新判定，
    # 于是「已纳管」和「后续迁移需要授权」是两句独立的话，不会因为后者而丢掉前者。
    pending = line[line.index(BASELINE_REVISION) + 1 :] if BASELINE_REVISION in line else ()
    destructive = tuple(rev for rev in pending if is_destructive(target, rev))
    notes = [
        f"结构与 baseline 指纹一致（{digest[:12]}…），"
        f"将 stamp 到 {BASELINE_REVISION} 而不重复建表。"
    ]
    if destructive and not allow_destructive:
        notes.append(
            "纳管之后还有破坏性迁移（" + "、".join(destructive) + "），"
            "它们需要 --allow-destructive 才会执行。"
        )
    return build(
        Decision.ADOPT_BASELINE,
        pending_revisions=pending,
        destructive_revisions=destructive,
        backup_required=census.total_business_rows > 0,
        notes=notes,
        next_action="journeypilot migrate",
    )


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #


def stamp(target: DatabaseTarget, revision: str) -> None:
    """写 alembic_version，不执行任何 DDL。

    **只有 `plan()` 判定 ADOPT_BASELINE 时才允许调用**，而那条路径以指纹逐字相同
    为前提。任何其他调用点都是 dev docs 02 §2.3 明令禁止的「把未知数据库伪装成
    最新版本」。
    """

    from alembic import command

    command.stamp(alembic_config(target), revision)
    logger.info("已将 %s 标记为 revision %s（未执行 DDL）", target.describe(), revision)


def upgrade(target: DatabaseTarget, revision: str = "head") -> None:
    from alembic import command

    command.upgrade(alembic_config(target), revision)
    logger.info("迁移完成：%s → %s", target.describe(), revision)


def downgrade(target: DatabaseTarget, revision: str) -> None:
    from alembic import command

    command.downgrade(alembic_config(target), revision)


def upgrade_sql(target: DatabaseTarget, revision: str = "head") -> None:
    """离线输出 SQL 而不执行（`migrate --sql`）。给「我想先看看它要干什么」。"""

    from alembic import command

    command.upgrade(alembic_config(target), revision, sql=True)


def current_revision(conn: Any) -> str | None:
    census = take_census(conn)
    return census.alembic_revision

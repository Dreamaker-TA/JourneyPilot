"""drop superseded legacy tables

Revision ID: 0002_drop_superseded
Revises: 0001_baseline
Create Date: 2026-08-17

## 这条迁移删什么

八张**没有任何代码读写**的遗留表。它们是更早几版的产物：`research_*` 与
`claim_evidence_links` 属于被 Delivery Bundle 取代的 research/claim 模型，
`itinerary_snapshots` / `itinerary_diffs` / `trip_run_versions` 属于被不可变
Bundle + CAS head 取代的 Living Itinerary 工作区，`user_skills` 属于一个已经
不存在的功能。

`init_db()` 早就不再创建它们，所以任何**新装**的库里没有它们；只有一路升级上来的
开发库里还留着。留着它们的代价不是磁盘，是**每一次 census 都要回答「这八张表是什么」**
—— 而「不知道是什么的表」会让「未知 schema，拒绝自动升级」这条判据变得没法用。

## 为什么可以硬删

产品尚未有用户（这是本轮 P0 的既定前提），且这八张表在整个仓里零引用
（`grep -rIl` 只在 `database.py` 的一句注释里命中 `research_runs`）。
所以不做 expand/contract 三步走，不留兼容视图，不留双读。

## 强制门禁六问

- **影响面**：只有 DB。没有任何代码读写这些表，所以后端/前端/Compose/配置不变。
- **旧数据路径**：`user_skills` 在开发库里有少量行，其余七张为空。这些行**会被删除**
  且无法从这条迁移恢复 —— 恢复路径是升级前的自动备份（非空库执行迁移前，
  `journeypilot migrate` 强制先备份并校验）。
- **失败注入**：单条迁移在一个事务里，中途失败整体回滚，`alembic_version` 不前进。
- **幂等**：`DROP TABLE IF EXISTS`。跑两次第二次什么都不做。
- **观察信号**：删除前后 doctor 的 `census.unmanaged_tables` 从八张变成空。
- **回滚**：**不可逆**。`downgrade()` 拒绝执行（见下），唯一回退路径是从备份恢复。
  release notes 必须写明：升级到 0002 之后回滚代码不需要回滚数据库，
  但回滚数据库只能靠备份。
"""

from __future__ import annotations

from alembic import op

revision = "0002_drop_superseded"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

#: 会删数据 → `journeypilot migrate` 需要 `--allow-destructive` 才执行。
#: 空库不受这道闸门约束：那里没有数据可丢，闸门保护的是**已有数据**。
destructive = True

#: 删掉的表和行都无法重建。
reversible = False

#: 被取代的遗留表。删除顺序照抄依赖方向的反向（先删引用方）——
#: 虽然 CASCADE 会处理，但显式顺序让「谁依赖谁」留在代码里。
SUPERSEDED_TABLES = (
    "claim_evidence_links",
    "research_evidence",
    "research_claims",
    "research_runs",
    "itinerary_diffs",
    "itinerary_snapshots",
    "trip_run_versions",
    "user_skills",
)


def upgrade() -> None:
    for table in SUPERSEDED_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


def downgrade() -> None:
    raise NotImplementedError(
        "0002 不可逆：被删掉的八张遗留表和其中的行无法从这条迁移重建。"
        "要回到 0002 之前的状态，只能用 `journeypilot restore <升级前的备份目录>`。"
        "重建空表壳子不是回滚 —— 它会让「已恢复」这句话变成假的。"
    )

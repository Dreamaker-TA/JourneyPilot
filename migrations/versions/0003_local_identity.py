"""fix identity to the local user

Revision ID: 0003_local_identity
Revises: 0002_drop_superseded
Create Date: 2026-08-17

## 这条迁移做什么

运行时身份收敛为固定的 `local`（`travel_agent/local_profile.py`）。`user_id` 列保留
为数据分区键，但取值只能是 `local`，默认值随之改写。

产品还没有用户，所以旧身份下的数据**直接删掉**，不做 census、选择合并与冲突规则：
那套编排是为「真实用户的历史数据」准备的，这里没有这样的数据。出厂语料
（`knowledge_*` 里非 `u_` 前缀的集合）与系统预设不属于任何身份，保留。

## 门禁

- **影响面**：DB、后端 API 形状、前端调用；`user_id` 不再出现在任何请求里。
- **旧数据路径**：删除，不可从本迁移恢复；恢复路径是升级前的自动备份。
- **失败注入**：单条迁移一个事务，中途失败整体回滚，`alembic_version` 不前进。
- **幂等**：DELETE 与 SET DEFAULT 重复执行结果相同。
- **观察信号**：迁移后各用户作用域表行数为 0；`user_id` 列默认值为 `local`。
- **回滚**：不可逆，`downgrade()` 拒绝执行。
"""

from __future__ import annotations

from alembic import op

revision = "0003_local_identity"
down_revision = "0002_drop_superseded"
branch_labels = None
depends_on = None

destructive = True
reversible = False

#: 删除顺序照依赖方向的反向；级联会处理其余表（trip_runs → run/bundle 全族，
#: chat_sessions → chat_session_events，memory_entities → memory_relations）。
_PURGE_STATEMENTS = (
    "DELETE FROM tool_execution_audits",
    "DELETE FROM trip_runs",
    "DELETE FROM chat_sessions",
    "DELETE FROM memory_forgetting_audits",
    "DELETE FROM memory_relations",
    "DELETE FROM memory_entities",
    "DELETE FROM memory_facts",
    "DELETE FROM user_profiles",
    "DELETE FROM travel_presets WHERE is_preset = FALSE",
    # 用户资料库按 owner 编码进物理集合名（`u_<user>__<logical>`）；出厂语料没有前缀。
    r"DELETE FROM knowledge_chunks WHERE collection LIKE 'u\_%'",
    r"DELETE FROM knowledge_documents WHERE collection LIKE 'u\_%'",
)

#: LangGraph 自己管的表：thread 指向已被删掉的会话，留着就是永远不会被读的孤儿。
_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")

_USER_ID_TABLES = (
    "user_profiles",
    "chat_sessions",
    "memory_facts",
    "memory_entities",
    "memory_relations",
    "memory_forgetting_audits",
    "travel_presets",
    "trip_runs",
)


def upgrade() -> None:
    for statement in _PURGE_STATEMENTS:
        op.execute(statement)
    for table in _CHECKPOINT_TABLES:
        op.execute(
            f"DO $$ BEGIN IF to_regclass('public.{table}') IS NOT NULL "
            f"THEN DELETE FROM {table}; END IF; END $$"
        )
    for table in _USER_ID_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN user_id SET DEFAULT 'local'")


def downgrade() -> None:
    raise NotImplementedError(
        "0003 不可逆：被删掉的旧身份数据无法从这条迁移重建。"
        "要回到 0003 之前的状态，只能用 `journeypilot restore <升级前的备份目录>`。"
    )

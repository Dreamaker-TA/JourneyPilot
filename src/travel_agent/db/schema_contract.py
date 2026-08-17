"""JourneyPilot 数据库合同的**唯一定义处**。

这里只声明「哪些东西属于合同」，不声明它们长什么样 —— 形状的定义处是
`migrations/versions/`，实际形状由 `fingerprint.py` 从活库读出来。三方各有一份
「表长什么样」的描述会立刻变成这个仓吃过亏的「一个角色两套值」，所以这份文件里
**没有一列一列的期望值**。

合同的三个消费者：

- `census.py`：这是不是一个 JourneyPilot 库（`PROBE_TABLES`）；
- `fingerprint.py`：指纹要扫哪些表（`MANAGED_TABLES`）；
- `report.py`：API 启动时只读校验要求哪些东西在（`MANAGED_TABLES` + `REQUIRED_EXTENSIONS`）。
"""

from __future__ import annotations

import re

# 指纹的合同版本。这个字符串进指纹摘要，所以改动它等于宣布「旧摘要全部失效」——
# 只有在指纹**算法**变化（扫了新的东西、换了归一化规则）时才递增，
# 表结构变化由 Alembic revision 表达，不动这里。
SCHEMA_CONTRACT_VERSION = "journeypilot.db.v1"

# 基线 revision。`census.py` 判定「有核心表但没有 alembic_version」的既有库时，
# 只允许 stamp 到这一个 revision，且必须先证明指纹与它一致。
BASELINE_REVISION = "0001_baseline"

# 迁移拥有的表。**只有出现在这里的表才受版本化迁移管辖**，也只有它们进指纹。
# 顺序无意义（指纹按名字排序），但保持按领域分组便于阅读。
MANAGED_TABLES: tuple[str, ...] = (
    # 身份与画像
    "user_profiles",
    # 会话历史
    "chat_sessions",
    "chat_session_events",
    # 记忆系统
    "memory_facts",
    "memory_entities",
    "memory_relations",
    "memory_forgetting_audits",
    # RAG 知识库
    "knowledge_documents",
    "knowledge_chunks",
    # 预设与产品配置
    "travel_presets",
    "product_configurations",
    # TripOps durable run
    "trip_runs",
    "trip_run_states",
    "trip_run_events",
    "trip_run_executions",
    "trip_run_commands",
    # Delivery v2
    "trip_workspace_v2_revisions",
    "fact_store_v2_revisions",
    "weather_context_v2_revisions",
    "delivery_bundles_v2",
    "delivery_bundle_heads_v2",
    "delivery_bundle_commits_v2",
    # 审计与台账
    "tool_execution_audits",
    "run_llm_calls",
)

# 「这是不是一个 JourneyPilot 库」的判据。取三张分属不同领域、且从第一版起就存在的表：
# 任何一张在，就不能把这个库当空库跑迁移。
PROBE_TABLES: tuple[str, ...] = ("user_profiles", "chat_sessions", "trip_runs")

# LangGraph checkpointer 的表。不进我们的迁移（langgraph 自带 `checkpoint_migrations`
# 版本表，两个 owner 会撞车），建立点在 `db/checkpoint_schema.py`。
LANGGRAPH_CHECKPOINT_TABLES: tuple[str, ...] = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)

# 别人拥有的表。它们出现在 public schema 里是**正常的**，不是 schema drift ——
# 所以指纹不扫它们，报告也不把它们算成「未知多余表」。
EXTERNALLY_OWNED_TABLES: dict[str, str] = {
    **{name: "langgraph-checkpoint-postgres" for name in LANGGRAPH_CHECKPOINT_TABLES},
    "alembic_version": "alembic",
}

# 核心扩展：缺一个就没有能跑的产品（向量检索、UUID 生成）。
REQUIRED_EXTENSIONS: tuple[str, ...] = ("vector", "pgcrypto")

# 可选能力：缺了要**显式降级并且说出来**，不能一边 fallback 一边宣称完整
# （dev docs 02 §10.1）。
OPTIONAL_EXTENSIONS: tuple[str, ...] = ("zhparser",)

# 中文词法检索用的 text search configuration 名。zhparser 不可用时退到 'simple'。
CHINESE_TEXT_SEARCH_CONFIG = "chinese"
FALLBACK_TEXT_SEARCH_CONFIG = "simple"

# 派生向量列。维度是**配置驱动**的，所以指纹里它们被归一化成占位符，实际维度另存一份
# （给 P0-D 的 embedding 合同校验用）。
EMBEDDING_VECTOR_COLUMNS: tuple[tuple[str, str], ...] = (
    ("knowledge_chunks", "embedding"),
    ("memory_facts", "embedding"),
    ("memory_entities", "embedding"),
)

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def sql_table_name_list(tables: tuple[str, ...] = MANAGED_TABLES) -> str:
    """把表名拼成 SQL 里的 `('a', 'b')` 字面量。

    **为什么不用绑定参数**：同一段 introspection SQL 要同时喂给 psycopg（`%(x)s`）和
    SQLAlchemy `text()`（`:x`），两种占位符语法不兼容 —— 参数化就意味着两份 SQL，
    而两份 SQL 就是两个「指纹到底扫了什么」的答案。表名全部是代码常量，这里再断言一次
    标识符形状，注入面为零。
    """

    for name in tables:
        if not _IDENTIFIER.match(name):
            raise ValueError(f"合同表名不是合法标识符，拒绝拼进 SQL: {name!r}")
    return "(" + ", ".join(f"'{name}'" for name in tables) + ")"

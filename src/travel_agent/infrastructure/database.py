"""
PostgreSQL + pgvector 数据库连接管理 (Infrastructure Layer)
使用 SQLAlchemy 异步引擎 + asyncpg 驱动。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, List, Optional, Tuple, Type, get_args

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import get_settings
from ..entities.user import TRAVEL_PREFERENCE_GROUPS, TravelPreference

logger = logging.getLogger(__name__)

# `user_profiles.preferences` 是 jsonb，读回时走 `TravelPreference(**blob)`，而那个模型是
# `extra="forbid"`。所以一旦某一轮从合同里删掉一个偏好字段，**库里旧行上的那个键就会让
# 每一次画像读取 500**。
# 修法不是让读取宽容（那是静默 fallback，等于把「库里存着合同外的东西」这件事藏起来），
# 而是把库里的行修成合同的样子。允许硬断裂：合同外的键直接删，不留双读。
# **允许的键只有一个定义处** —— `TravelPreference` 自己；这里现算，所以下一次删字段
# 不需要再改这段 SQL。
#
# **拒绝陌生键的不只顶层那一个模型。** `default_origin` 是 `PlaceIdentity`，它也是
# `extra="forbid"`，而它是首次使用必填的 —— 从它身上删一个字段，每一个填过常用出发地的
# 人都会重新撞上一模一样的 500，而只扫顶层的清理**看不见**这件事。所以这里扫的是
# `preference_contract_shape()` 给出的整棵树，不是一层。
#
# 同族其余四处「存下来的 blob 整块交给 extra=forbid 的模型解」各有自己的说法，不归这里管：
# Delivery Bundle 在解之前先比合同印章、印章不符抛 `BundleContractSuperseded`
# （`delivery_bundle_store.py::bundle_from_row`）；provider 快照在 Redis 里，解不动就删键
# 当 cache miss（`provider_snapshot_cache.py::lookup`）；产品配置的种子在代码侧，发布命令
# 一句 `DO UPDATE` 就能重写（`preset/product_config.py`）；`trip_runs.controlled_trip_identity`
# 的真相同样只在库里（`ControlledTripIdentity.model_validate`，见 `workflows/weather_context.py`
# 与 `workflows/minimum_delivery_draft.py`），但它是「一趟已锁定的行程该不该被改形状」这个
# 产品问题，不是画像读取。
#
# `updated_at` 不出现在 SET 里：这是一次形状收敛，不是用户改了偏好，那一列不该动。
_PRUNE_UNKNOWN_PREFERENCE_KEYS_SQL = """
    UPDATE user_profiles
    SET preferences = COALESCE(
            (
                SELECT jsonb_object_agg(kv.key, kv.value)
                FROM jsonb_each(preferences) AS kv
                WHERE kv.key = ANY(:contract_keys)
            ),
            '{}'::jsonb
        )
    WHERE EXISTS (
        SELECT 1 FROM jsonb_object_keys(preferences) AS k
        WHERE k <> ALL(:contract_keys)
    )
"""

# 同一件事在树的下一层：`preferences #> path` 那个对象上只留白名单里的键。
# 顶层那一条先跑 —— 若整个 `default_origin` 键本身就不在合同里，它已经被顶层删掉，
# 这一条自然什么都找不到。
_PRUNE_UNKNOWN_NESTED_PREFERENCE_KEYS_SQL = """
    UPDATE user_profiles
    SET preferences = jsonb_set(
            preferences,
            CAST(:json_path AS text[]),
            COALESCE(
                (
                    SELECT jsonb_object_agg(kv.key, kv.value)
                    FROM jsonb_each(preferences #> CAST(:json_path AS text[])) AS kv
                    WHERE kv.key = ANY(:contract_keys)
                ),
                '{}'::jsonb
            )
        )
    WHERE jsonb_typeof(preferences #> CAST(:json_path AS text[])) = 'object'
      AND EXISTS (
          SELECT 1
          FROM jsonb_object_keys(preferences #> CAST(:json_path AS text[])) AS k
          WHERE k <> ALL(:contract_keys)
      )
"""


# 同一条二选一，换成**取值**那一层：`TravelPreference` 现在还要求六组的取值出自
# `TRAVEL_PREFERENCE_GROUPS`，所以「库里存着一个表外的值」和「存着一个合同外的键」一样，
# 会让每一次画像读取 500。要么模型容得下表外的值（那就等于承认界面画不出来的值也能存），
# 要么启动时把它们清掉。选后者，与键那一层同一副语法。
#
# **清掉而不是改写**：表外的值没有一个「正确的对应值」可以映（`中档` 是 `舒适型` 还是
# `品质型`？猜一个就是替用户决定），所以按「重新进入，删除旧的」——
# 值删掉，用户回到那一屏重新点选，而不是拿到一个别人替他选的档位。
#
# `updated_at` 同样不出现在 SET 里：这是一次取值收敛，不是用户改了偏好。
_CONVERGE_MULTI_PREFERENCE_VALUES_SQL = """
    UPDATE user_profiles
    SET preferences = jsonb_set(
            preferences,
            CAST(:json_path AS text[]),
            COALESCE(
                (
                    SELECT jsonb_agg(v.value)
                    FROM jsonb_array_elements_text(
                        preferences #> CAST(:json_path AS text[])
                    ) AS v(value)
                    WHERE v.value = ANY(:options)
                ),
                '[]'::jsonb
            )
        )
    WHERE jsonb_typeof(preferences #> CAST(:json_path AS text[])) = 'array'
      AND EXISTS (
          SELECT 1
          FROM jsonb_array_elements_text(
              preferences #> CAST(:json_path AS text[])
          ) AS v(value)
          WHERE v.value <> ALL(:options)
      )
"""

# 单选组：空串是「没选」，是合法值，所以收敛的目标就是空串。
_CONVERGE_SCALAR_PREFERENCE_VALUE_SQL = """
    UPDATE user_profiles
    SET preferences = jsonb_set(preferences, CAST(:json_path AS text[]), '""'::jsonb)
    WHERE jsonb_typeof(preferences #> CAST(:json_path AS text[])) = 'string'
      AND (preferences #>> CAST(:json_path AS text[])) <> ''
      AND (preferences #>> CAST(:json_path AS text[])) <> ALL(:options)
"""


def preference_option_vocabularies() -> List[Tuple[List[str], bool, List[str]]]:
    """六组偏好的 `(json 路径, 是否多选, 合法取值)`，现算自唯一的定义处。

    与 `preference_contract_shape()` 同一个理由现算：选项表在
    `entities/user.py::TRAVEL_PREFERENCE_GROUPS`，这里再抄一份就是「一个角色两套值」，
    而静默胜出的会是这一份（它是启动时真跑的那一份）。
    """

    return [
        ([group.key], group.multi, list(group.options))
        for group in TRAVEL_PREFERENCE_GROUPS
    ]


def contract_submodel_of(annotation: Any) -> Optional[Type[BaseModel]]:
    """这个字段注解里那个「值是一个对象」的子模型，没有就是 None。

    `Optional[PlaceIdentity]` 要拆开看，`List[str]` 不算。这是 `preference_contract_shape()`
    与它的判据**共用的同一枚镜片** —— 两处各写一份「怎么算子模型」正是这个仓吃过亏的
    「一个角色两套值」。

    **不在这里筛 `extra="forbid"`。** 中间那一层宽容、更深一层拒绝，同样会炸；筛在这里
    就会把那种树剪断。谁拒绝陌生键是模型自己的事，扫要扫全棵。
    """

    for candidate in (annotation, *get_args(annotation)):
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def preference_contract_shape() -> List[Tuple[List[str], List[str]]]:
    """`user_profiles.preferences` 这棵树上每个对象节点：`(json 路径, 允许的键)`。

    现算自唯一的定义处（`TravelPreference` 自己），顶层在前、深的在后 —— 清理按这个顺序
    跑。**只走「值是一个对象」的那种嵌套**：如果哪天有人把一个合同模型放进列表或字典值里，
    这份形状必须跟着扩展，否则那条路径的键不会被扫到。
    """

    shape: List[Tuple[List[str], List[str]]] = []
    queue: List[Tuple[List[str], Type[BaseModel]]] = [([], TravelPreference)]
    while queue:
        path, model = queue.pop(0)
        shape.append((path, sorted(model.model_fields)))
        for name, field in model.model_fields.items():
            submodel = contract_submodel_of(field.annotation)
            if submodel is not None:
                queue.append((path + [name], submodel))
    return shape


def preference_contract_keys() -> list[str]:
    """`user_profiles.preferences` 顶层允许出现的键，现算自唯一的定义处。"""
    return sorted(TravelPreference.model_fields)

_CHAT_SESSION_TABLE_COLUMNS: set[str] = {
    "session_id",
    "user_id",
    "title",
    "status",
    "mode",
    "last_message_preview",
    "pending_clarify",
    "anchor_summary",
    "compression_count",
    "compaction_boundary_event_order",
    "message_count",
    "created_at",
    "updated_at",
}

_DELIVERY_V2_TABLE_COLUMNS: dict[str, set[str]] = {
    "trip_workspace_v2_revisions": {
        "run_id", "workspace_revision", "content_hash", "snapshot", "created_at",
    },
    "fact_store_v2_revisions": {
        "run_id", "fact_data_revision", "content_hash", "snapshot", "created_at",
    },
    "weather_context_v2_revisions": {
        "run_id", "weather_data_revision", "content_hash", "snapshot", "created_at",
    },
    "delivery_bundles_v2": {
        "bundle_id", "run_id", "workspace_revision", "fact_data_revision",
        "weather_data_revision", "manifest", "bundle", "created_at",
    },
    "delivery_bundle_heads_v2": {
        "run_id", "current_bundle_id", "workspace_revision", "fact_data_revision",
        "weather_data_revision", "updated_at",
    },
    "delivery_bundle_commits_v2": {
        "run_id", "idempotency_key", "request_digest", "commit_kind",
        "base_bundle_id", "result_bundle_id",
        "inverse_patch", "metadata", "created_at",
    },
}


async def _assert_chat_session_schema(conn: AsyncConnection) -> None:
    """Reject extra or missing session columns instead of mutating an old panel schema."""
    result = await conn.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'chat_sessions'
            """
        )
    )
    actual = {row["column_name"] for row in result.mappings()}
    if actual != _CHAT_SESSION_TABLE_COLUMNS:
        missing = sorted(_CHAT_SESSION_TABLE_COLUMNS - actual)
        unexpected = sorted(actual - _CHAT_SESSION_TABLE_COLUMNS)
        raise RuntimeError(
            "Chat session schema contract mismatch: "
            f"missing={missing}, unexpected={unexpected}. "
            "Rebuild the chat_sessions table before starting the application."
        )


async def _assert_delivery_v2_schema(conn: AsyncConnection) -> None:
    """Reject drift in the direct-replacement v2 delivery persistence contract."""
    for table_name, expected in _DELIVERY_V2_TABLE_COLUMNS.items():
        result = await conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        )
        actual = {row["column_name"] for row in result.mappings()}
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise RuntimeError(
                f"Delivery v2 schema contract mismatch for {table_name}: "
                f"missing={missing}, unexpected={unexpected}. "
                "Rebuild the v2 delivery tables before starting the application."
            )


# 全局引擎和 Session 工厂
_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker] = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database.url,
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
            pool_pre_ping=True,
            echo=settings.debug,
        )
    return _engine


def get_session_factory() -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话的异步上下文管理器"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """
    初始化数据库：启用 pgvector 扩展，创建必要的表结构。
    应在应用启动时调用一次。
    """
    settings = get_settings()
    engine = get_engine()

    async with engine.begin() as conn:
        # 启用必要的 PostgreSQL 扩展
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        # zhparser 属于当前数据库合同，不能只依赖首次创建 Docker volume 的脚本；
        # 重建 schema 后应用初始化也必须恢复中文全文检索配置。
        await conn.execute(text("""
            DO $$
            BEGIN
                CREATE EXTENSION IF NOT EXISTS zhparser;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_ts_config WHERE cfgname = 'chinese'
                ) THEN
                    CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
                    ALTER TEXT SEARCH CONFIGURATION chinese
                        ADD MAPPING FOR n,v,a,i,e,l,j,t WITH simple;
                END IF;
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'zhparser initialization unavailable: %', SQLERRM;
            END
            $$;
        """))
        ts_config_result = await conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_ts_config WHERE cfgname = 'chinese') AS has_chinese")
        )
        has_chinese_ts = bool(ts_config_result.mappings().first()["has_chinese"])
        text_search_config = "chinese" if has_chinese_ts else "simple"
        if not has_chinese_ts:
            logger.warning("PostgreSQL text search config 'chinese' 不可用，RAG lexical search 回退到 'simple'")

        # 用户画像表
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id      TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                preferences  JSONB NOT NULL DEFAULT '{}',
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        # 把存量 profile 行上合同外的偏好键删掉，整棵树扫一遍
        # （说明见 _PRUNE_UNKNOWN_PREFERENCE_KEYS_SQL）。
        for json_path, contract_keys in preference_contract_shape():
            if json_path:
                statement = _PRUNE_UNKNOWN_NESTED_PREFERENCE_KEYS_SQL
                params = {"json_path": json_path, "contract_keys": contract_keys}
            else:
                statement = _PRUNE_UNKNOWN_PREFERENCE_KEYS_SQL
                params = {"contract_keys": contract_keys}
            pruned = await conn.execute(text(statement), params)
            if pruned.rowcount:
                logger.info(
                    "user_profiles.preferences%s 清理了 %d 行合同外的偏好键（合同：%s）",
                    "".join(f".{part}" for part in json_path),
                    pruned.rowcount,
                    ", ".join(contract_keys),
                )

        # 再把选项表之外的**取值**收敛掉（说明见 _CONVERGE_MULTI_PREFERENCE_VALUES_SQL）。
        # 键那一步先跑：某一组本身已经不在合同里时，这里自然什么都找不到。
        for json_path, multi, options in preference_option_vocabularies():
            statement = (
                _CONVERGE_MULTI_PREFERENCE_VALUES_SQL
                if multi
                else _CONVERGE_SCALAR_PREFERENCE_VALUE_SQL
            )
            converged = await conn.execute(
                text(statement), {"json_path": json_path, "options": options}
            )
            if converged.rowcount:
                logger.info(
                    "user_profiles.preferences.%s 清掉了 %d 行选项表之外的取值（可选：%s）",
                    ".".join(json_path),
                    converged.rowcount,
                    "、".join(options),
                )

        # 正式会话历史。旅行交付由不可变 Delivery Bundle 持有，不在会话表复制面板。
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id           TEXT PRIMARY KEY,
                user_id              TEXT NOT NULL,
                title                TEXT NOT NULL,
                status               TEXT NOT NULL DEFAULT 'active',
                mode                 TEXT NOT NULL DEFAULT 'fast',
                last_message_preview TEXT NOT NULL DEFAULT '',
                pending_clarify      JSONB,
                anchor_summary       JSONB DEFAULT NULL,
                compression_count    INTEGER NOT NULL DEFAULT 0,
                compaction_boundary_event_order INTEGER NOT NULL DEFAULT 0,
                message_count        INTEGER NOT NULL DEFAULT 0,
                created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        # 压缩点：被当前 Anchor 摘要覆盖到的最后一条会话事件。没有它，载入器不知道
        # 哪一段历史已经折叠进摘要，于是摘要是**净加**在 prompt 上的（实测压缩后那次
        # 调用 input 102,121 > 压缩前 101,405 —— 压缩是净亏的）。
        await conn.execute(text("""
            ALTER TABLE chat_sessions
            ADD COLUMN IF NOT EXISTS compaction_boundary_event_order INTEGER NOT NULL DEFAULT 0
        """))
        await _assert_chat_session_schema(conn)
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated
            ON chat_sessions (user_id, updated_at DESC)
        """))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_session_events (
                event_id     BIGSERIAL PRIMARY KEY,
                session_id   TEXT NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
                event_order  INTEGER NOT NULL,
                event_type   TEXT NOT NULL,
                payload      JSONB NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_session_events_order
            ON chat_session_events (session_id, event_order)
        """))

        # 记忆系统：原子事实表
        dim = settings.embedding.dimensions
        await conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS memory_facts (
                fact_id     BIGSERIAL PRIMARY KEY,
                user_id     TEXT NOT NULL,
                session_id  TEXT NOT NULL DEFAULT '',
                content     TEXT NOT NULL,
                category    TEXT NOT NULL DEFAULT 'preference',
                importance  SMALLINT NOT NULL DEFAULT 5,
                embedding   vector({dim}),
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_facts_user_created
            ON memory_facts (user_id, created_at DESC)
        """))
        await conn.execute(text("""
            ALTER TABLE memory_facts
            ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ
        """))
        await conn.execute(text("""
            ALTER TABLE memory_facts
            ADD COLUMN IF NOT EXISTS retention_category TEXT NOT NULL DEFAULT 'standard'
        """))
        await conn.execute(text("""
            ALTER TABLE memory_facts
            ADD COLUMN IF NOT EXISTS retention_policy JSONB NOT NULL DEFAULT '{}'
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_facts_user_expires_created
            ON memory_facts (user_id, expires_at, created_at DESC)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_facts_user_category
            ON memory_facts (user_id, category, created_at DESC)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_facts_expires_at
            ON memory_facts (expires_at)
            WHERE expires_at IS NOT NULL
        """))
        try:
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_memory_facts_embedding
                ON memory_facts USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 50)
            """))
        except Exception as e:
            logger.warning(f"memory_facts 向量索引创建跳过（数据量不足或已存在）: {e}")

        # 记忆系统：知识图谱实体节点表
        await conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS memory_entities (
                entity_id   BIGSERIAL PRIMARY KEY,
                user_id     TEXT NOT NULL,
                name        TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                properties  JSONB NOT NULL DEFAULT '{{}}',
                embedding   vector({dim}),
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, name, entity_type)
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_entities_user_type
            ON memory_entities (user_id, entity_type)
        """))

        # 记忆系统：知识图谱关系边表
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_relations (
                relation_id  BIGSERIAL PRIMARY KEY,
                user_id      TEXT NOT NULL,
                source_id    BIGINT NOT NULL REFERENCES memory_entities(entity_id) ON DELETE CASCADE,
                target_id    BIGINT NOT NULL REFERENCES memory_entities(entity_id) ON DELETE CASCADE,
                relation     TEXT NOT NULL,
                confidence   FLOAT NOT NULL DEFAULT 1.0,
                evidence     TEXT NOT NULL DEFAULT '',
                valid_from   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                source_session TEXT NOT NULL DEFAULT '',
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        # `valid_until` 是「图谱矛盾消解」留下的墓碑列。删除语义是物理删除，一行在表里
        # 就是有效的 —— 墓碑列没有意义。列上已经打过墓碑的行只有一个来源 —— 下面那段
        # 去重迁移；它们对任何读者都不可见，所以随列一起删。**允许硬断裂**，不留双读。
        await conn.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'memory_relations'
                      AND column_name = 'valid_until'
                ) THEN
                    DELETE FROM memory_relations WHERE valid_until IS NOT NULL;
                    ALTER TABLE memory_relations DROP COLUMN valid_until;
                END IF;
            END
            $$;
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_relations_user
            ON memory_relations (user_id)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_relations_source
            ON memory_relations (source_id)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_relations_target
            ON memory_relations (target_id)
        """))
        await conn.execute(text("""
            WITH ranked AS (
                SELECT relation_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id, source_id, target_id, relation
                           ORDER BY confidence DESC, relation_id DESC
                       ) AS rn
                FROM memory_relations
            )
            DELETE FROM memory_relations
            WHERE relation_id IN (SELECT relation_id FROM ranked WHERE rn > 1)
        """))
        # 旧名叫 uq_memory_relations_active，是一个 `WHERE valid_until IS NULL` 的部分索引；
        # 列删掉时它会跟着消失，这里显式 DROP 只为覆盖手工建过同名索引的机器。
        # 换名是因为「active」这个词此刻已经不指任何东西 —— 表里的每一行都是有效的。
        await conn.execute(text("DROP INDEX IF EXISTS uq_memory_relations_active"))
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_relations_edge
            ON memory_relations (user_id, source_id, target_id, relation)
        """))

        # user_profiles 表新增 auto_portrait 列（系统推理画像快照）
        try:
            await conn.execute(text("""
                ALTER TABLE user_profiles
                ADD COLUMN IF NOT EXISTS auto_portrait TEXT NOT NULL DEFAULT ''
            """))
        except Exception as e:
            logger.debug(f"auto_portrait 列已存在或创建跳过: {e}")

        # JP-08-07：Memory Retention / Forgetting / Delete audit boundary。
        # 只保存删除范围、affected counts 和安全 metadata，不保存 raw memory content。
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_forgetting_audits (
                request_id               TEXT PRIMARY KEY,
                user_id                  TEXT NOT NULL,
                scope                    TEXT NOT NULL,
                category                 TEXT,
                fact_id                  TEXT,
                status                   TEXT NOT NULL,
                affected_facts           INTEGER NOT NULL DEFAULT 0,
                affected_entities        INTEGER NOT NULL DEFAULT 0,
                affected_relations       INTEGER NOT NULL DEFAULT 0,
                affected_profiles        INTEGER NOT NULL DEFAULT 0,
                affected_session_anchors INTEGER NOT NULL DEFAULT 0,
                boundary                 JSONB NOT NULL DEFAULT '{}',
                metadata                 JSONB NOT NULL DEFAULT '{}',
                created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_forgetting_audits_user_created
            ON memory_forgetting_audits (user_id, created_at DESC)
        """))

        # RAG 知识库向量表
        await conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id               BIGSERIAL PRIMARY KEY,
                collection       TEXT NOT NULL DEFAULT 'default',
                content          TEXT NOT NULL,
                original_content TEXT,
                source           TEXT NOT NULL DEFAULT '',
                metadata         JSONB NOT NULL DEFAULT '{{}}',
                embedding        vector({dim}),
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        # Contextual Chunking: 为已存在的表添加 original_content 列（幂等）
        try:
            await conn.execute(text("""
                ALTER TABLE knowledge_chunks
                ADD COLUMN IF NOT EXISTS original_content TEXT
            """))
        except Exception as e:
            logger.debug(f"original_content 列已存在或创建跳过: {e}")
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_knowledge_collection
            ON knowledge_chunks (collection)
        """))
        # 添加 tsvector 列用于 PostgreSQL lexical full-text 检索（Hybrid Search）
        try:
            await conn.execute(text("""
                ALTER TABLE knowledge_chunks
                ADD COLUMN IF NOT EXISTS tsv tsvector
                    GENERATED ALWAYS AS (
                        to_tsvector('{text_search_config}', coalesce(content, ''))
                    ) STORED
            """.format(text_search_config=text_search_config)))
        except Exception as e:
            logger.debug(f"tsvector 列已存在或创建跳过: {e}")
        # GIN 索引加速全文检索
        try:
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_knowledge_tsv
                ON knowledge_chunks USING gin(tsv)
            """))
        except Exception as e:
            logger.debug(f"GIN 索引创建跳过: {e}")
        # 创建 IVFFlat 向量索引（数据量 < 10 万时建议使用 ivfflat）
        try:
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_knowledge_embedding
                ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
            """))
        except Exception as e:
            logger.warning(f"向量索引创建跳过（可能数据量不足或已存在）: {e}")

        # 一篇资料的正文 —— **这是它的定义处**，`knowledge_chunks` 是它的派生投影。
        #
        # 为什么必须有这张表：段是带重叠切出来的（`chunk_overlap`），contextual 分块还会
        # 在每段头上加一句 LLM 写的上下文前缀。把段拼回去得不到原文，只能得到一份接缝处
        # 重复的近似品 —— 所以「查看/编辑一篇资料」如果读段，读到的就不是用户写下的东西。
        # 一篇资料在 `(collection, source)` 上唯一：同名重新入库是**替换**，不是再长一份。
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                id          BIGSERIAL PRIMARY KEY,
                collection  TEXT NOT NULL,
                source      TEXT NOT NULL,
                content     TEXT NOT NULL,
                metadata    JSONB NOT NULL DEFAULT '{}',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (collection, source)
            )
        """))

        # 旅行预设与产品配置是运行时唯一事实来源。当前项目未上线，直接使用
        # 语义明确的当前表名，不创建历史别名或兼容视图。
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS travel_presets (
                id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                icon TEXT DEFAULT 'compass',
                category TEXT DEFAULT 'custom',
                instructions TEXT NOT NULL DEFAULT '',
                constraints JSONB DEFAULT '{}',
                is_preset BOOLEAN DEFAULT FALSE,
                usage_count INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_travel_presets_user_id
            ON travel_presets (user_id)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_travel_presets_system
            ON travel_presets (is_preset) WHERE is_preset = TRUE
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS product_configurations (
                config_key TEXT PRIMARY KEY,
                config JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))

        # JP-08-01：TripOps durable run 生命周期。
        # 与 research_runs / chat_session_events 分离：这里只保存业务 run record、
        # latest state projection 和 audit-safe event timeline。
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trip_runs (
                run_id               TEXT PRIMARY KEY,
                session_id           TEXT NOT NULL DEFAULT '',
                user_id              TEXT NOT NULL DEFAULT 'anonymous',
                mode                 TEXT NOT NULL DEFAULT 'deep',
                status               TEXT NOT NULL DEFAULT 'created',
                request_message_id   TEXT NOT NULL DEFAULT '',
                assistant_message_id TEXT NOT NULL DEFAULT '',
                parent_run_id        TEXT,
                current_node         TEXT,
                resume_token_hash    TEXT,
                resume_policy        TEXT NOT NULL DEFAULT 'clarify_only',
                controlled_trip_identity JSONB,
                checkpoint_ns        TEXT NOT NULL DEFAULT '',
                last_checkpoint_id   TEXT,
                last_error_code      TEXT,
                last_error_message   TEXT,
                attempt              INTEGER NOT NULL DEFAULT 1,
                created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                started_at           TIMESTAMPTZ,
                completed_at         TIMESTAMPTZ,
                cancelled_at         TIMESTAMPTZ
            )
        """))
        await conn.execute(text("""
            ALTER TABLE trip_runs
            ADD COLUMN IF NOT EXISTS checkpoint_ns TEXT NOT NULL DEFAULT ''
        """))
        await conn.execute(text("""
            ALTER TABLE trip_runs
            ADD COLUMN IF NOT EXISTS last_checkpoint_id TEXT
        """))
        await conn.execute(text("""
            ALTER TABLE trip_runs
            ADD COLUMN IF NOT EXISTS controlled_trip_identity JSONB
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_trip_runs_user_updated
            ON trip_runs (user_id, updated_at DESC)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_trip_runs_session_updated
            ON trip_runs (session_id, updated_at DESC)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_trip_runs_status_updated
            ON trip_runs (status, updated_at DESC)
        """))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trip_run_states (
                run_id                        TEXT PRIMARY KEY REFERENCES trip_runs(run_id) ON DELETE CASCADE,
                status                        TEXT NOT NULL DEFAULT 'created',
                current_node                  TEXT,
                completed_nodes               JSONB NOT NULL DEFAULT '[]',
                latest_state_summary          JSONB NOT NULL DEFAULT '{}',
                completion_audit              JSONB NOT NULL DEFAULT '{}',
                pending_user_choice           JSONB,
                trace_event_count             INTEGER NOT NULL DEFAULT 0,
                pending_monitor_trigger_count INTEGER NOT NULL DEFAULT 0,
                last_error                    JSONB,
                updated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        # Completion diagnostics are deliberately separate from the normal
        # user-facing state projection. Existing databases need the additive
        # migration as well as the create-table definition above.
        await conn.execute(text("""
            ALTER TABLE trip_run_states
            ADD COLUMN IF NOT EXISTS completion_audit JSONB NOT NULL DEFAULT '{}'
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_trip_run_states_status
            ON trip_run_states (status)
        """))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trip_run_events (
                event_id   BIGSERIAL PRIMARY KEY,
                run_id     TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
                sequence   INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload    JSONB NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (run_id, sequence)
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_trip_run_events_run_sequence
            ON trip_run_events (run_id, sequence)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_trip_run_events_type
            ON trip_run_events (event_type)
        """))

        # JourneyPilot delivery v2: immutable base snapshots, immutable bundles,
        # and one CAS-protected current pointer. These tables do not read, copy,
        # or migrate the superseded Living Itinerary workspace contract.
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trip_workspace_v2_revisions (
                run_id             TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
                workspace_revision INTEGER NOT NULL,
                content_hash       TEXT NOT NULL,
                snapshot           JSONB NOT NULL,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (run_id, workspace_revision)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS fact_store_v2_revisions (
                run_id             TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
                fact_data_revision INTEGER NOT NULL,
                content_hash       TEXT NOT NULL,
                snapshot           JSONB NOT NULL,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (run_id, fact_data_revision)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS weather_context_v2_revisions (
                run_id                TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
                weather_data_revision INTEGER NOT NULL,
                content_hash          TEXT NOT NULL,
                snapshot              JSONB NOT NULL,
                created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (run_id, weather_data_revision)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS delivery_bundles_v2 (
                bundle_id            TEXT PRIMARY KEY,
                run_id               TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
                workspace_revision   INTEGER NOT NULL,
                fact_data_revision   INTEGER NOT NULL,
                weather_data_revision INTEGER NOT NULL,
                manifest             JSONB NOT NULL,
                bundle               JSONB NOT NULL,
                created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (run_id, workspace_revision, fact_data_revision, weather_data_revision),
                FOREIGN KEY (run_id, workspace_revision)
                    REFERENCES trip_workspace_v2_revisions(run_id, workspace_revision),
                FOREIGN KEY (run_id, fact_data_revision)
                    REFERENCES fact_store_v2_revisions(run_id, fact_data_revision),
                FOREIGN KEY (run_id, weather_data_revision)
                    REFERENCES weather_context_v2_revisions(run_id, weather_data_revision)
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_delivery_bundles_v2_run_created
            ON delivery_bundles_v2 (run_id, created_at DESC)
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS delivery_bundle_heads_v2 (
                run_id                TEXT PRIMARY KEY REFERENCES trip_runs(run_id) ON DELETE CASCADE,
                current_bundle_id      TEXT NOT NULL REFERENCES delivery_bundles_v2(bundle_id),
                workspace_revision     INTEGER NOT NULL,
                fact_data_revision     INTEGER NOT NULL,
                weather_data_revision  INTEGER NOT NULL,
                updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS delivery_bundle_commits_v2 (
                run_id                 TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
                idempotency_key         TEXT NOT NULL,
                request_digest          TEXT NOT NULL,
                commit_kind            TEXT NOT NULL,
                base_bundle_id          TEXT,
                result_bundle_id        TEXT NOT NULL REFERENCES delivery_bundles_v2(bundle_id),
                inverse_patch           JSONB,
                metadata                JSONB NOT NULL DEFAULT '{}',
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (run_id, idempotency_key)
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_delivery_bundle_commits_v2_run_created
            ON delivery_bundle_commits_v2 (run_id, created_at DESC)
        """))
        await _assert_delivery_v2_schema(conn)

        # JP-08-06：Production Tool Gateway durable audit。
        # 只保存 audit-safe envelope 字段和 policy metadata，不保存 raw args/result/provider payload。
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tool_execution_audits (
                audit_id              TEXT PRIMARY KEY,
                run_id                TEXT REFERENCES trip_runs(run_id) ON DELETE SET NULL,
                tool_name             TEXT NOT NULL,
                server_name           TEXT,
                source_type           TEXT NOT NULL DEFAULT 'tool',
                category              TEXT NOT NULL DEFAULT 'other',
                permission_class      TEXT NOT NULL DEFAULT 'read_only',
                operation_sensitivity TEXT NOT NULL DEFAULT 'low',
                status                TEXT NOT NULL,
                gateway_decision      TEXT NOT NULL,
                args_digest           TEXT NOT NULL,
                result_digest         TEXT NOT NULL DEFAULT '',
                untrusted_content     BOOLEAN NOT NULL DEFAULT FALSE,
                quarantined           BOOLEAN NOT NULL DEFAULT FALSE,
                fallback_from         TEXT,
                fallback_to           TEXT,
                degradation_reason    TEXT,
                error                 TEXT,
                evidence_allowed      BOOLEAN NOT NULL DEFAULT FALSE,
                metadata              JSONB NOT NULL DEFAULT '{}',
                created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_tool_audits_run_created
            ON tool_execution_audits (run_id, created_at DESC)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_tool_audits_tool_created
            ON tool_execution_audits (tool_name, created_at DESC)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_tool_audits_status_created
            ON tool_execution_audits (status, created_at DESC)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_tool_audits_run_risky_status
            ON tool_execution_audits (run_id, status)
            WHERE status IN ('blocked', 'degraded', 'failed')
        """))

        # LLM 成本台账。捕获层 drain 出的每次调用落一行，
        # 写入时按价格表快照计算 cost_usd（价格表后改不追溯重算）。字段命名对齐 OTel GenAI。
        # audit-safe：只存 token 计数/成本/时延，绝不存 prompt / 响应内容。
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS run_llm_calls (
                id                      TEXT PRIMARY KEY,
                run_id                  TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
                node                    TEXT,
                agent                   TEXT,
                tier                    TEXT,
                provider                TEXT,
                model_request           TEXT NOT NULL DEFAULT '',
                model_response          TEXT,
                input_tokens            INTEGER,
                output_tokens           INTEGER,
                cached_input_tokens     INTEGER,
                reasoning_output_tokens INTEGER,
                cost_usd                DOUBLE PRECISION,
                estimated               BOOLEAN NOT NULL DEFAULT FALSE,
                start_ts                TIMESTAMPTZ,
                end_ts                  TIMESTAMPTZ,
                ttft_ms                 DOUBLE PRECISION,
                latency_ms              DOUBLE PRECISION,
                status                  TEXT NOT NULL DEFAULT 'ok',
                stream                  BOOLEAN NOT NULL DEFAULT FALSE,
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_run_llm_calls_run_start
            ON run_llm_calls (run_id, start_ts)
        """))

        logger.info("数据库初始化完成（pgvector + 所有业务表）")


async def close_db() -> None:
    """关闭数据库连接池"""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
    logger.info("数据库连接池已关闭")

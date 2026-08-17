"""baseline current schema

Revision ID: 0001_baseline
Revises: None
Create Date: 2026-08-17

## 这条迁移是什么

commit b867a3a 时 `init_db()` 在一个空库上跑完之后的**最终形态**，一次建成。

它不是「历史的第一步」，而是「历史被折叠成的一个点」。`init_db()` 里那些
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`、`DROP COLUMN valid_until`、
`DROP INDEX uq_memory_relations_active` 都是从更早形状爬上来的台阶；产品还没有
用户，没有任何库需要从那些更早的形状爬 —— 所以台阶不进 baseline，
只有台阶的终点进。

**等价性不靠人眼保证**：`tests/db/test_migration_baseline.py` 在两个临时库上分别跑
`init_db()` 和 `alembic upgrade head`，断言两份 schema 指纹逐字相同。改这个文件而不
改 `init_db()`（或反过来）会让那个测试红。

## 强制门禁六问

- **影响面**：只有 DB。后端/前端/Compose/配置/文档不因这条迁移改变行为
  （`init_db()` 在 P0-A 里原样保留，见 ADR-P0-03 的分阶段落地）。
- **旧数据路径**：这条迁移只在**空库**上执行。已经有核心表但没有 `alembic_version`
  的库走 adoption：先比指纹，一致才 `stamp 0001`，不重复建表
  （`db/migrate.py::plan`）。指纹不一致的库拒绝自动升级。
- **失败注入**：单条迁移在一个事务里（`env.py` 的 `transaction_per_migration`），
  PostgreSQL 事务性 DDL 保证中途失败后库回到执行前，`alembic_version` 不前进。
- **幂等**：全部 `IF NOT EXISTS`。重复执行不报错、不重复建。
- **观察信号**：失败时 CLI 非零退出并打印 alembic 报错；API 侧
  `GET /api/health/ready` 的 `database_schema` 会报缺表清单。
- **回滚**：`downgrade()` 删掉本迁移建立的所有表。这会**删光业务数据** ——
  它存在是为了让测试能验证 up/down 对称，不是给生产用的回退路径。

## 两处随环境变化的地方

- **向量维度**取自 `embedding.dimensions` 配置。三张表的 `embedding` 列共用它。
- **`knowledge_chunks.tsv` 的 text search config**：zhparser 装上并注册了 `chinese`
  就用 `chinese`，否则退到 `simple`，并且**说出来**（不静默假装中文词法完整）。
  生成列的表达式一旦建好就固定了，切换 config 需要一条专门的重建迁移
  （dev docs 02 §10.2）。
"""

from __future__ import annotations

import logging

from alembic import context, op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

destructive = False
reversible = True

logger = logging.getLogger("alembic.runtime.migration")


def _embedding_dimensions() -> int:
    from travel_agent.config import get_settings

    return int(get_settings().embedding.dimensions)


def _resolve_text_search_config() -> str:
    """`chinese` 可用就用它，否则 `simple`，并把降级说出来。

    **离线模式（`migrate --sql`）没有连接可问**，只能取 `chinese` —— 这个产品自带的
    Compose 镜像装了 zhparser，上面那段 DO 块会在应用时把配置建好。这个假设会作为
    一句 SQL 注释留在输出里：离线 SQL 是**给人看的预览**，不是拿去在一个没有 zhparser
    的环境里手工执行的脚本。悄悄取一个值而不说，才是把预览变成陷阱的那一步。
    """

    if context.is_offline_mode():
        op.execute(
            "-- 离线生成：无法探测 zhparser，此脚本按 'chinese' 生成 "
            "knowledge_chunks.tsv。目标库缺少 zhparser 时这一句会失败 —— "
            "那种环境请直接跑 `journeypilot migrate`，它会探测并退到 'simple'。"
        )
        return "chinese"

    conn = op.get_bind()
    has_chinese = conn.exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_ts_config WHERE cfgname = 'chinese')"
    ).scalar()
    if has_chinese:
        return "chinese"
    logger.warning(
        "PostgreSQL text search config 'chinese' 不可用（zhparser 未安装）："
        "knowledge_chunks.tsv 将使用 'simple' 分词，中文词法检索能力降级。"
        "启用后需要一条专门的重建迁移，改运行时变量不会改写已建好的生成列。"
    )
    return "simple"


def _install_extensions() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    # zhparser 是可选能力：装不上不阻断迁移，但要留下 NOTICE。
    op.execute(
        """
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
        """
    )


def upgrade() -> None:
    dim = _embedding_dimensions()
    _install_extensions()
    text_search_config = _resolve_text_search_config()

    # ---------------------------------------------------------------- 身份与画像
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id       TEXT PRIMARY KEY,
            display_name  TEXT NOT NULL DEFAULT '',
            preferences   JSONB NOT NULL DEFAULT '{}',
            auto_portrait TEXT NOT NULL DEFAULT '',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    # ---------------------------------------------------------------- 会话历史
    # 旅行交付由不可变 Delivery Bundle 持有，不在会话表复制面板。
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated
        ON chat_sessions (user_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_session_events (
            event_id     BIGSERIAL PRIMARY KEY,
            session_id   TEXT NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
            event_order  INTEGER NOT NULL,
            event_type   TEXT NOT NULL,
            payload      JSONB NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_session_events_order
        ON chat_session_events (session_id, event_order)
        """
    )

    # ---------------------------------------------------------------- 记忆系统
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS memory_facts (
            fact_id            BIGSERIAL PRIMARY KEY,
            user_id            TEXT NOT NULL,
            session_id         TEXT NOT NULL DEFAULT '',
            content            TEXT NOT NULL,
            category           TEXT NOT NULL DEFAULT 'preference',
            importance         SMALLINT NOT NULL DEFAULT 5,
            embedding          vector({dim}),
            expires_at         TIMESTAMPTZ,
            retention_category TEXT NOT NULL DEFAULT 'standard',
            retention_policy   JSONB NOT NULL DEFAULT '{{}}',
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_facts_user_created
        ON memory_facts (user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_facts_user_expires_created
        ON memory_facts (user_id, expires_at, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_facts_user_category
        ON memory_facts (user_id, category, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_facts_expires_at
        ON memory_facts (expires_at)
        WHERE expires_at IS NOT NULL
        """
    )
    # IVFFlat 在空表上建索引是合法的（列表数偏大只影响召回质量，不影响正确性），
    # 所以这里**不吞异常**：建不出来说明 pgvector 有真问题，那时该让迁移失败。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_facts_embedding
        ON memory_facts USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 50)
        """
    )

    op.execute(
        f"""
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_entities_user_type
        ON memory_entities (user_id, entity_type)
        """
    )

    # 图谱关系边。**没有 `valid_until` 墓碑列**：删除语义是物理删除，一行在表里就是
    # 有效的，所以 `uq_memory_relations_edge` 是无条件唯一索引而不是部分索引。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_relations (
            relation_id    BIGSERIAL PRIMARY KEY,
            user_id        TEXT NOT NULL,
            source_id      BIGINT NOT NULL REFERENCES memory_entities(entity_id) ON DELETE CASCADE,
            target_id      BIGINT NOT NULL REFERENCES memory_entities(entity_id) ON DELETE CASCADE,
            relation       TEXT NOT NULL,
            confidence     FLOAT NOT NULL DEFAULT 1.0,
            evidence       TEXT NOT NULL DEFAULT '',
            valid_from     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source_session TEXT NOT NULL DEFAULT '',
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_relations_user ON memory_relations (user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_relations_source ON memory_relations (source_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_relations_target ON memory_relations (target_id)"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_relations_edge
        ON memory_relations (user_id, source_id, target_id, relation)
        """
    )

    # 遗忘/删除审计。只保存删除范围与影响计数，不保存 raw memory content。
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_forgetting_audits_user_created
        ON memory_forgetting_audits (user_id, created_at DESC)
        """
    )

    # ---------------------------------------------------------------- RAG 知识库
    # 一篇资料的正文在 knowledge_documents，`knowledge_chunks` 是它的派生投影：
    # 段是带重叠切出来的，contextual 分块还会在段头加 LLM 写的上下文前缀，
    # 把段拼回去只能得到一份接缝重复的近似品。reindex 的原始事实来源是前者。
    op.execute(
        """
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
        """
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id               BIGSERIAL PRIMARY KEY,
            collection       TEXT NOT NULL DEFAULT 'default',
            content          TEXT NOT NULL,
            original_content TEXT,
            source           TEXT NOT NULL DEFAULT '',
            metadata         JSONB NOT NULL DEFAULT '{{}}',
            embedding        vector({dim}),
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            tsv              tsvector GENERATED ALWAYS AS (
                                 to_tsvector('{text_search_config}', coalesce(content, ''))
                             ) STORED
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_collection ON knowledge_chunks (collection)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_tsv ON knowledge_chunks USING gin(tsv)")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_knowledge_embedding
        ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
        """
    )

    # ---------------------------------------------------------------- 预设与产品配置
    op.execute(
        """
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
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_travel_presets_user_id ON travel_presets (user_id)"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_travel_presets_system
        ON travel_presets (is_preset) WHERE is_preset = TRUE
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS product_configurations (
            config_key TEXT PRIMARY KEY,
            config JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    # ---------------------------------------------------------------- TripOps durable run
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trip_runs_user_updated
        ON trip_runs (user_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trip_runs_session_updated
        ON trip_runs (session_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trip_runs_status_updated
        ON trip_runs (status, updated_at DESC)
        """
    )

    op.execute(
        """
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
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trip_run_states_status ON trip_run_states (status)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS trip_run_events (
            event_id   BIGSERIAL PRIMARY KEY,
            run_id     TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
            sequence   INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload    JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (run_id, sequence)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trip_run_events_run_sequence
        ON trip_run_events (run_id, sequence)
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trip_run_events_type ON trip_run_events (event_type)"
    )

    # ---------------------------------------------------------------- Delivery v2
    # 不可变基线快照 + 不可变 bundle + 一个 CAS 保护的 current 指针。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS trip_workspace_v2_revisions (
            run_id             TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
            workspace_revision INTEGER NOT NULL,
            content_hash       TEXT NOT NULL,
            snapshot           JSONB NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (run_id, workspace_revision)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_store_v2_revisions (
            run_id             TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
            fact_data_revision INTEGER NOT NULL,
            content_hash       TEXT NOT NULL,
            snapshot           JSONB NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (run_id, fact_data_revision)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_context_v2_revisions (
            run_id                TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
            weather_data_revision INTEGER NOT NULL,
            content_hash          TEXT NOT NULL,
            snapshot              JSONB NOT NULL,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (run_id, weather_data_revision)
        )
        """
    )
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_delivery_bundles_v2_run_created
        ON delivery_bundles_v2 (run_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_bundle_heads_v2 (
            run_id                TEXT PRIMARY KEY REFERENCES trip_runs(run_id) ON DELETE CASCADE,
            current_bundle_id      TEXT NOT NULL REFERENCES delivery_bundles_v2(bundle_id),
            workspace_revision     INTEGER NOT NULL,
            fact_data_revision     INTEGER NOT NULL,
            weather_data_revision  INTEGER NOT NULL,
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_delivery_bundle_commits_v2_run_created
        ON delivery_bundle_commits_v2 (run_id, created_at DESC)
        """
    )

    # ---------------------------------------------------------------- 审计与台账
    # 只保存 audit-safe envelope 与 policy metadata，不保存 raw args/result/provider payload。
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tool_audits_run_created
        ON tool_execution_audits (run_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tool_audits_tool_created
        ON tool_execution_audits (tool_name, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tool_audits_status_created
        ON tool_execution_audits (status, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tool_audits_run_risky_status
        ON tool_execution_audits (run_id, status)
        WHERE status IN ('blocked', 'degraded', 'failed')
        """
    )

    # LLM 成本台账。audit-safe：只存 token 计数/成本/时延，绝不存 prompt / 响应内容。
    op.execute(
        """
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
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_llm_calls_run_start ON run_llm_calls (run_id, start_ts)"
    )


def downgrade() -> None:
    """删掉 baseline 建立的所有表。**会删光业务数据。**

    存在的唯一理由是让测试能验证 up/down 对称。生产回退路径是从备份恢复
    （`journeypilot restore`），不是这个函数。

    扩展和 `chinese` text search configuration 不删：它们可能被同一个数据库里
    别人的东西用着，而且重装它们不丢数据。
    """

    from travel_agent.db.schema_contract import MANAGED_TABLES

    for table in reversed(MANAGED_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')

"""指纹的归一化规则和差异报告。

指纹是这一整个 PR 的判据来源，所以它自己的行为必须被钉住：**哪些差异算差异**，
以及**差异报告能不能让人看懂差在哪**（§3.2 要求诊断可执行，不是「摘要不符」）。
"""

from __future__ import annotations

import pytest

from travel_agent.db import migrate
from travel_agent.db.connection import connect
from travel_agent.db.fingerprint import (
    EMBEDDING_DIM_PLACEHOLDER,
    TEXT_SEARCH_CONFIG_PLACEHOLDER,
    build_fingerprint,
    canonical_json,
    diff_fingerprints,
    fingerprint_digest,
    fingerprint_sync,
)

pytestmark = pytest.mark.postgres


def test_vector_dimension_is_normalized_but_recorded(temp_database, settings):
    """配置维度被折叠成占位符，实际维度另存 —— 换维度不该让摘要变成「未知 schema」。"""

    migrate.upgrade(temp_database)
    dimensions = settings.embedding.dimensions

    with connect(temp_database) as conn:
        fingerprint = fingerprint_sync(conn, embedding_dimensions=dimensions)

    columns = {col["name"]: col for col in fingerprint["tables"]["knowledge_chunks"]["columns"]}
    assert columns["embedding"]["type"] == f"vector({EMBEDDING_DIM_PLACEHOLDER})"
    assert fingerprint["embedding_columns"]["knowledge_chunks.embedding"] == dimensions
    # 实际维度不在摘要里 —— 否则改配置就等于结构不认识了。
    assert str(dimensions) not in canonical_json(fingerprint)


def test_mismatched_dimension_is_not_normalized_away(temp_database, settings):
    """如果实际列维度与传入的配置维度**不同**，它必须留在摘要里让人看见。

    这条是 P0-D（embedding 合同）的前置：归一化的目的是折叠「合法的环境差异」，
    不是把「配置说 1536、库里是 1024」这种真问题一起藏掉。
    """

    migrate.upgrade(temp_database)
    real = settings.embedding.dimensions

    with connect(temp_database) as conn:
        wrong = fingerprint_sync(conn, embedding_dimensions=real + 512)

    columns = {col["name"]: col for col in wrong["tables"]["memory_facts"]["columns"]}
    assert columns["embedding"]["type"] == f"vector({real})", (
        "维度不匹配时不该被折叠成占位符"
    )
    assert EMBEDDING_DIM_PLACEHOLDER not in canonical_json(wrong)


def test_text_search_config_is_normalized(temp_database, settings):
    """`tsv` 生成表达式里的 config 名折叠成占位符，实际取值另报。

    zhparser 装没装是环境事实。让它进摘要，等于在一台没装 zhparser 的机器上
    把整个数据库判成「未知结构，拒绝升级」。
    """

    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        fingerprint = fingerprint_sync(
            conn, embedding_dimensions=settings.embedding.dimensions
        )

    columns = {col["name"]: col for col in fingerprint["tables"]["knowledge_chunks"]["columns"]}
    assert columns["tsv"]["generated"] is not None
    assert TEXT_SEARCH_CONFIG_PLACEHOLDER in columns["tsv"]["generated"]
    assert fingerprint["optional_capabilities"]["text_search_config"] in {"chinese", "simple"}
    # 可选能力不进摘要。
    assert "optional_capabilities" not in canonical_json(fingerprint)


def test_column_order_does_not_change_the_digest(temp_database, settings):
    """物理列顺序不同、其余相同 → 同一个摘要。

    全仓 `SELECT *` 都按名取值，没有读者依赖 attnum。
    """

    migrate.upgrade(temp_database)
    dimensions = settings.embedding.dimensions

    with connect(temp_database) as conn:
        first = fingerprint_sync(conn, embedding_dimensions=dimensions)
        with conn.cursor() as cur:
            # 换一种物理顺序：删掉再加回去，attnum 变到末尾。
            cur.execute("ALTER TABLE trip_runs DROP COLUMN parent_run_id")
            cur.execute("ALTER TABLE trip_runs ADD COLUMN parent_run_id TEXT")
        second = fingerprint_sync(conn, embedding_dimensions=dimensions)

    assert fingerprint_digest(first) == fingerprint_digest(second), (
        "列顺序改变了摘要：" + "\n".join(diff_fingerprints(first, second))
    )


def test_diff_names_what_actually_differs():
    """差异报告要指名道姓，不能只说「不一致」。"""

    def table(columns, indexes=()):
        return {
            "schema_contract_version": "journeypilot.db.v1",
            "tables": {
                "t": {
                    "columns": list(columns),
                    "constraints": [],
                    "indexes": [{"name": name, "definition": definition} for name, definition in indexes],
                }
            },
            "missing_tables": [],
            "extensions": ["vector"],
        }

    column = {"name": "a", "type": "text", "not_null": True, "default": None, "generated": None}
    expected = table([column], indexes=[("idx_a", "CREATE INDEX idx_a ON t (a)")])
    actual = table(
        [
            {**column, "not_null": False},
            {"name": "extra", "type": "text", "not_null": False, "default": None, "generated": None},
        ]
    )

    problems = diff_fingerprints(expected, actual)
    joined = "\n".join(problems)
    assert "t.a.not_null" in joined
    assert "t.extra" in joined and "多出" in joined
    assert "idx_a" in joined and "缺索引" in joined


def test_digest_is_stable_across_key_insertion_order():
    """摘要只取决于内容，不取决于 dict 的构造顺序。"""

    base = {
        "schema_contract_version": "v",
        "tables": {},
        "missing_tables": [],
        "extensions": ["vector", "pgcrypto"],
    }
    shuffled = {
        "extensions": ["vector", "pgcrypto"],
        "missing_tables": [],
        "tables": {},
        "schema_contract_version": "v",
    }
    assert fingerprint_digest(base) == fingerprint_digest(shuffled)


def test_build_fingerprint_is_pure():
    """纯函数：同一批行两次装出同一份指纹（同步/异步适配器共用它的前提）。"""

    rows = {
        "columns": [
            {
                "table_name": "memory_facts",
                "column_name": "embedding",
                "ordinal": 7,
                "data_type": "vector(1024)",
                "not_null": False,
                "column_expr": None,
                "is_generated": False,
            }
        ],
        "constraints": [
            {"table_name": "memory_facts", "constraint_type": "p", "definition": "PRIMARY KEY (fact_id)"}
        ],
        "indexes": [],
        "extensions": [{"name": "vector"}, {"name": "plpgsql"}],
        "text_search_configs": [{"name": "simple"}],
    }
    first = build_fingerprint(rows, embedding_dimensions=1024)
    second = build_fingerprint(rows, embedding_dimensions=1024)
    assert first == second
    # plpgsql 不是核心扩展合同的一部分，不进指纹。
    assert first["extensions"] == ["vector"]
    assert first["embedding_columns"] == {"memory_facts.embedding": 1024}

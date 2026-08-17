"""Schema 指纹：从活库读出结构，归一化，算 canonical JSON + SHA-256。

指纹回答一个问题：**这个数据库的结构是不是某个已知 revision 的结构。**
它存在的理由是 dev docs 02 §2.3 那条禁令 —— 不许对来历不明的库执行
`alembic stamp head`，因为那会把未知结构伪装成最新版本。

## 归一化：哪些差异不算差异

两处结构合法地随环境变化，指纹必须把它们折叠掉，否则同一份 revision 在两台机器上
会算出两个摘要，「摘要不符」就再也不代表任何东西：

- **向量列维度**由 `embedding.dimensions` 配置决定 → 归一化成 `vector(<dim>)`，
  实际维度另存进 `embedding_columns`（不进摘要）；
- **`knowledge_chunks.tsv` 的生成表达式**取决于 zhparser 装没装 → 归一化成
  `<text_search_config>`，实际取值另存进 `optional_capabilities`（不进摘要）。

## 不进摘要的东西

- **约束名**：`UNIQUE (a, b)` 写在 CREATE TABLE 里时名字由 PostgreSQL 生成，
  同一份结构换一种等价写法就会换个名字。摘要按 `(类型, 定义)` 排序去名。
- **PG18 的 `contype = 'n'`**（NOT NULL 落进 pg_constraint）：它与 `attnotnull`
  说同一件事，且是版本相关的，留着会让同一份结构在 PG16/PG18 上摘要不同。
- **索引名反过来必须进摘要**：它们在 DDL 里被显式命名，而代码里有
  `CREATE INDEX IF NOT EXISTS <name>` 按名判断存在性 —— 名字是合同的一部分。
- **列的物理顺序**：全仓 37 处 `SELECT *` 都经 `.mappings()` 按名取值，没有一处按位置
  取列，所以 attnum 顺序不是任何读者依赖的东西。指纹按列名排序 —— 否则「同一张表
  用 CREATE TABLE 一次建好」和「先建表再 ALTER ADD COLUMN」会算出两个摘要，
  而那个差异对代码不可见。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .schema_contract import (
    EMBEDDING_VECTOR_COLUMNS,
    MANAGED_TABLES,
    OPTIONAL_EXTENSIONS,
    REQUIRED_EXTENSIONS,
    SCHEMA_CONTRACT_VERSION,
    sql_table_name_list,
)

EMBEDDING_DIM_PLACEHOLDER = "<embedding_dimensions>"
TEXT_SEARCH_CONFIG_PLACEHOLDER = "<text_search_config>"

_VECTOR_TYPE = re.compile(r"^vector\((\d+)\)$")
_TSVECTOR_CONFIG = re.compile(r"to_tsvector\('([a-z_][a-z0-9_]*)'::regconfig")


def introspection_queries() -> dict[str, str]:
    """五条 introspection SQL。**同步与异步适配器共用这一份**，不许各写一份。

    全部无绑定参数（理由见 `schema_contract.sql_table_name_list`），所以同一个字符串
    既能交给 psycopg 也能交给 SQLAlchemy `text()`。
    """

    tables = sql_table_name_list()
    return {
        "columns": f"""
            SELECT c.relname   AS table_name,
                   a.attname   AS column_name,
                   a.attnum    AS ordinal,
                   format_type(a.atttypid, a.atttypmod) AS data_type,
                   a.attnotnull AS not_null,
                   pg_get_expr(d.adbin, d.adrelid) AS column_expr,
                   a.attgenerated <> '' AS is_generated
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
            WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname IN {tables}
            ORDER BY c.relname, a.attnum
        """,
        # contype 'n' 被排除：PG17+ 把 NOT NULL 也记成约束，与 attnotnull 重复。
        "constraints": f"""
            SELECT c.relname AS table_name,
                   con.contype::text AS constraint_type,
                   pg_get_constraintdef(con.oid) AS definition
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname IN {tables} AND con.contype <> 'n'
            ORDER BY c.relname, con.contype, pg_get_constraintdef(con.oid)
        """,
        # 约束背后的索引不重复记一遍（约束定义已经描述了它）。
        "indexes": f"""
            SELECT c.relname AS table_name,
                   i.relname AS index_name,
                   pg_get_indexdef(x.indexrelid) AS definition
            FROM pg_index x
            JOIN pg_class c ON c.oid = x.indrelid
            JOIN pg_class i ON i.oid = x.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_constraint con ON con.conindid = x.indexrelid
            WHERE n.nspname = 'public' AND c.relname IN {tables} AND con.oid IS NULL
            ORDER BY c.relname, i.relname
        """,
        "extensions": """
            SELECT extname AS name FROM pg_extension ORDER BY extname
        """,
        "text_search_configs": """
            SELECT cfgname AS name FROM pg_catalog.pg_ts_config ORDER BY cfgname
        """,
    }


def _normalize_type(data_type: str, embedding_dimensions: int) -> str:
    match = _VECTOR_TYPE.match(data_type)
    if match and int(match.group(1)) == embedding_dimensions:
        return f"vector({EMBEDDING_DIM_PLACEHOLDER})"
    return data_type


def _normalize_expr(expr: str | None) -> str | None:
    """折叠掉生成表达式里的 text search config 名（实际取值另存进环境事实）。"""
    if expr is None:
        return expr
    return _TSVECTOR_CONFIG.sub(
        f"to_tsvector('{TEXT_SEARCH_CONFIG_PLACEHOLDER}'::regconfig", expr
    )


def build_fingerprint(
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    embedding_dimensions: int,
) -> dict[str, Any]:
    """把 `introspection_queries()` 的结果装成指纹。**纯函数** —— 不碰数据库。

    同步/异步两个适配器都落到这里，所以「指纹是什么」只有一个答案。
    """

    extensions = {row["name"] for row in rows["extensions"]}
    ts_configs = {row["name"] for row in rows["text_search_configs"]}

    # 归一化 tsvector 表达式要知道**实际生效的那个 config 名**。
    # 直接从列表达式里读，而不是从「zhparser 装没装」推断：生成列一旦建好，
    # 表达式就固定了，后来卸掉扩展也不会改写它 —— 推断会说谎，读取不会。
    text_search_config: str | None = None
    for row in rows["columns"]:
        if row["table_name"] == "knowledge_chunks" and row["column_name"] == "tsv":
            match = _TSVECTOR_CONFIG.search(row["column_expr"] or "")
            if match:
                text_search_config = match.group(1)

    tables: dict[str, Any] = {}
    embedding_columns: dict[str, Any] = {}

    for row in rows["columns"]:
        table = tables.setdefault(
            row["table_name"], {"columns": [], "constraints": [], "indexes": []}
        )
        raw_type = row["data_type"]
        expr = row["column_expr"]
        generated = bool(row["is_generated"])
        table["columns"].append(
            {
                "name": row["column_name"],
                "type": _normalize_type(raw_type, embedding_dimensions),
                "not_null": bool(row["not_null"]),
                "default": None if generated else expr,
                "generated": _normalize_expr(expr) if generated else None,
            }
        )
        if (row["table_name"], row["column_name"]) in EMBEDDING_VECTOR_COLUMNS:
            match = _VECTOR_TYPE.match(raw_type)
            embedding_columns[f"{row['table_name']}.{row['column_name']}"] = (
                int(match.group(1)) if match else None
            )

    for row in rows["constraints"]:
        table = tables.get(row["table_name"])
        if table is None:
            continue
        table["constraints"].append(
            {"type": row["constraint_type"], "definition": row["definition"]}
        )

    for row in rows["indexes"]:
        table = tables.get(row["table_name"])
        if table is None:
            continue
        table["indexes"].append({"name": row["index_name"], "definition": row["definition"]})

    for table in tables.values():
        table["columns"].sort(key=lambda item: item["name"])
        table["constraints"].sort(key=lambda item: (item["type"], item["definition"]))
        table["indexes"].sort(key=lambda item: item["name"])

    return {
        "schema_contract_version": SCHEMA_CONTRACT_VERSION,
        "tables": {name: tables[name] for name in sorted(tables)},
        "missing_tables": sorted(set(MANAGED_TABLES) - set(tables)),
        "extensions": sorted(extensions & set(REQUIRED_EXTENSIONS)),
        # 以下三项**不进摘要**：它们是环境事实，不是结构合同。
        "optional_capabilities": {
            name: name in extensions for name in OPTIONAL_EXTENSIONS
        }
        | {
            "text_search_config": text_search_config,
            "chinese_text_search_available": "chinese" in ts_configs,
        },
        "embedding_columns": dict(sorted(embedding_columns.items())),
    }


# 摘要只覆盖「结构合同」那几项。环境事实（可选能力、实际向量维度）刻意留在摘要之外，
# 否则装不装 zhparser 就会变成「未知 schema，拒绝启动」。
_DIGEST_KEYS = ("schema_contract_version", "tables", "missing_tables", "extensions")


def normative_only(fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    """只留进摘要的那几项 —— 存档用这个形状。

    环境事实（装没装 zhparser、实际向量维度）留在存档里会制造**假的 git 差异**：
    在一台没有 zhparser 的机器上重新生成，文件会变，但结构一个字节都没变。
    那种差异会训练人忽略这份文件的变化，而它恰恰是不该被忽略的那一份。
    """

    return {key: fingerprint[key] for key in _DIGEST_KEYS}


def canonical_json(fingerprint: Mapping[str, Any]) -> str:
    """摘要的输入串。`sort_keys` + 固定分隔符 = 跨进程可复现。"""
    return json.dumps(
        {key: fingerprint[key] for key in _DIGEST_KEYS},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def fingerprint_digest(fingerprint: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(fingerprint).encode("utf-8")).hexdigest()


def fingerprint_sync(conn: Any, *, embedding_dimensions: int) -> dict[str, Any]:
    """从 psycopg3 连接读指纹（CLI / Alembic 侧）。"""

    rows: dict[str, list[dict[str, Any]]] = {}
    for key, sql in introspection_queries().items():
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [desc[0] for desc in cur.description]
            rows[key] = [dict(zip(columns, record)) for record in cur.fetchall()]
    return build_fingerprint(rows, embedding_dimensions=embedding_dimensions)


async def fingerprint_async(conn: Any, *, embedding_dimensions: int) -> dict[str, Any]:
    """从 SQLAlchemy `AsyncConnection` 读指纹（API 只读校验侧）。

    全部是 `SELECT`，所以 API 进程调用它不违反 ADR-P0-03。
    """

    from sqlalchemy import text

    rows: dict[str, list[dict[str, Any]]] = {}
    for key, sql in introspection_queries().items():
        result = await conn.execute(text(sql))
        rows[key] = [dict(record) for record in result.mappings()]
    return build_fingerprint(rows, embedding_dimensions=embedding_dimensions)


def diff_fingerprints(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> list[str]:
    """人能读的结构差异清单。空列表 = 摘要一致。

    存在的理由：`report.py` 和 CLI 都要在拒绝启动时说清「差在哪、下一步跑什么」，
    而「摘要 abc123 != def456」不是可执行的诊断（dev docs 02 §3.2）。
    """

    problems: list[str] = []

    if expected.get("schema_contract_version") != actual.get("schema_contract_version"):
        problems.append(
            f"指纹合同版本不同：期望 {expected.get('schema_contract_version')}，"
            f"实际 {actual.get('schema_contract_version')}"
        )

    expected_tables: Mapping[str, Any] = expected.get("tables", {})
    actual_tables: Mapping[str, Any] = actual.get("tables", {})

    for name in sorted(set(expected_tables) - set(actual_tables)):
        problems.append(f"缺表：{name}")
    for name in sorted(set(actual_tables) - set(expected_tables)):
        problems.append(f"多出合同外的表：{name}")

    for name in sorted(set(expected_tables) & set(actual_tables)):
        exp, act = expected_tables[name], actual_tables[name]

        exp_cols = {col["name"]: col for col in exp["columns"]}
        act_cols = {col["name"]: col for col in act["columns"]}
        for col in sorted(set(exp_cols) - set(act_cols)):
            problems.append(f"{name}.{col}：缺列")
        for col in sorted(set(act_cols) - set(exp_cols)):
            problems.append(f"{name}.{col}：多出合同外的列")
        for col in sorted(set(exp_cols) & set(act_cols)):
            for field in ("type", "not_null", "default", "generated"):
                if exp_cols[col][field] != act_cols[col][field]:
                    problems.append(
                        f"{name}.{col}.{field}：期望 {exp_cols[col][field]!r}，"
                        f"实际 {act_cols[col][field]!r}"
                    )

        exp_cons = {(c["type"], c["definition"]) for c in exp["constraints"]}
        act_cons = {(c["type"], c["definition"]) for c in act["constraints"]}
        for kind, definition in sorted(exp_cons - act_cons):
            problems.append(f"{name}：缺约束 [{kind}] {definition}")
        for kind, definition in sorted(act_cons - exp_cons):
            problems.append(f"{name}：多出合同外的约束 [{kind}] {definition}")

        exp_idx = {i["name"]: i["definition"] for i in exp["indexes"]}
        act_idx = {i["name"]: i["definition"] for i in act["indexes"]}
        for idx in sorted(set(exp_idx) - set(act_idx)):
            problems.append(f"{name}：缺索引 {idx}")
        for idx in sorted(set(act_idx) - set(exp_idx)):
            problems.append(f"{name}：多出合同外的索引 {idx}")
        for idx in sorted(set(exp_idx) & set(act_idx)):
            if exp_idx[idx] != act_idx[idx]:
                problems.append(
                    f"{name}：索引 {idx} 定义不同："
                    f"期望 {exp_idx[idx]!r}，实际 {act_idx[idx]!r}"
                )

    exp_ext = set(expected.get("extensions", []))
    act_ext = set(actual.get("extensions", []))
    for ext in sorted(exp_ext - act_ext):
        problems.append(f"缺核心扩展：{ext}")

    return problems

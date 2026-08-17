"""备份的三条校验，以及「校验必须能抓到坏备份」。

dev docs 02 §5.2：「只看到 `pg_dump` exit code 0 还不够。」所以这里不只测「备份成功」，
更要测**篡改后的备份被识别为坏的** —— 一个抓不到损坏的校验器等于没有校验器。
"""

from __future__ import annotations

import json

import pytest

from travel_agent.db import migrate
from travel_agent.db.backup import (
    BACKUP_FORMAT_VERSION,
    DUMP_FILENAME,
    MANIFEST_FILENAME,
    create_backup,
    list_backups,
    load_manifest,
    prune_automatic_backups,
    verify_backup,
)
from travel_agent.db.connection import connect

pytestmark = pytest.mark.postgres


def _seed(target) -> None:
    """放一点带中文、JSONB 和向量的数据 —— §14.2 要求备份覆盖这些类型。"""

    with connect(target) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_profiles (user_id, display_name, preferences) "
                "VALUES ('local', '本地用户', %s)",
                (json.dumps({"travel_style": ["深度游"]}),),
            )
            cur.execute(
                "INSERT INTO knowledge_documents (collection, source, content) "
                "VALUES ('travel_tips', '青岛', '崂山与栈桥')"
            )


def test_backup_is_created_and_verified(temp_database, settings, scratch_dir):
    migrate.upgrade(temp_database)
    _seed(temp_database)

    with connect(temp_database) as conn:
        result = create_backup(
            conn,
            temp_database,
            embedding_dimensions=settings.embedding.dimensions,
            output_root=scratch_dir,
            kind="manual",
            label="unit",
        )

    manifest = result.manifest
    assert manifest["backup_format_version"] == BACKUP_FORMAT_VERSION
    assert manifest["migration_revision"] == migrate.revision_line()[-1]
    assert manifest["row_counts"]["user_profiles"] == 1
    assert manifest["total_business_rows"] >= 2
    assert manifest["verified"] == {
        "dump_non_empty": True,
        "pg_restore_list_parsed": True,
        "checksums_written": True,
    }
    # dump 必须真的有内容 —— 一个 0 字节文件也能让 pg_dump 返回 0。
    assert (result.directory / DUMP_FILENAME).stat().st_size > 0
    assert manifest["schema_fingerprint_sha256"]
    # 每个文件都有 checksum，且 manifest 自己不在里面（它记录别人）。
    names = {item["name"] for item in manifest["files"]}
    assert DUMP_FILENAME in names
    assert MANIFEST_FILENAME not in names

    assert verify_backup(result.directory) == []


def test_verify_catches_a_truncated_dump(temp_database, settings, scratch_dir):
    """截断的备份必须被识别出来。这是备份最常见的坏法。"""

    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        result = create_backup(
            conn,
            temp_database,
            embedding_dimensions=settings.embedding.dimensions,
            output_root=scratch_dir,
        )

    dump = result.directory / DUMP_FILENAME
    original = dump.read_bytes()
    dump.write_bytes(original[: len(original) // 2])

    problems = verify_backup(result.directory)
    assert problems, "截断的 dump 通过了校验 —— 校验器没有起作用"
    assert any("大小不符" in problem or "SHA-256" in problem for problem in problems)


def test_verify_catches_a_tampered_dump_of_the_same_size(
    temp_database, settings, scratch_dir
):
    """同样大小但内容被改过 → SHA-256 抓出来。只比大小是不够的。"""

    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        result = create_backup(
            conn,
            temp_database,
            embedding_dimensions=settings.embedding.dimensions,
            output_root=scratch_dir,
        )

    dump = result.directory / DUMP_FILENAME
    data = bytearray(dump.read_bytes())
    data[len(data) // 2] ^= 0xFF
    dump.write_bytes(bytes(data))

    problems = verify_backup(result.directory)
    assert any("SHA-256" in problem for problem in problems), problems


def test_verify_rejects_a_directory_without_manifest(scratch_dir):
    (scratch_dir / "not-a-backup").mkdir()
    problems = verify_backup(scratch_dir / "not-a-backup")
    assert problems and MANIFEST_FILENAME in problems[0]


def test_prune_keeps_manual_backups(temp_database, settings, scratch_dir):
    """保留策略只删自动备份。手工备份是用户说过「这份我要留着」的。"""

    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        for index in range(3):
            create_backup(
                conn,
                temp_database,
                embedding_dimensions=settings.embedding.dimensions,
                output_root=scratch_dir,
                kind="automatic",
                label=f"auto{index}",
            )
        manual = create_backup(
            conn,
            temp_database,
            embedding_dimensions=settings.embedding.dimensions,
            output_root=scratch_dir,
            kind="manual",
            label="keepme",
        )

    doomed = prune_automatic_backups(scratch_dir, keep=1, dry_run=True)
    assert len(doomed) == 2, doomed
    assert all("keepme" not in path for path in doomed)
    # dry_run 不删任何东西。
    assert len(list_backups(scratch_dir)) == 4

    prune_automatic_backups(scratch_dir, keep=1)
    remaining = {entry["path"] for entry in list_backups(scratch_dir)}
    assert str(manual.directory) in remaining
    assert len(remaining) == 2


def test_manifest_records_the_pg_client_strategy(temp_database, settings, scratch_dir):
    """manifest 要记下「谁导出的、什么版本」—— 恢复时要靠它判断兼容范围。"""

    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        result = create_backup(
            conn,
            temp_database,
            embedding_dimensions=settings.embedding.dimensions,
            output_root=scratch_dir,
        )

    manifest = load_manifest(result.directory)
    assert manifest["pg_dump"]["strategy"] in {"local", "docker"}
    assert manifest["pg_dump"]["client_major"] >= manifest["database"]["postgres_major"], (
        "客户端版本低于服务端时不该产出备份"
    )


def test_redaction_keeps_structure_and_drops_secrets():
    """脱敏保留结构，Secret 只留 <set> / <unset> 这一个比特。

    备份目录里那份 config.redacted.yaml 与 `journeypilot config show` 用**同一份**
    规则（`config/redaction.py`），所以这一条同时钉住了两处。
    """

    from travel_agent.config import redact

    payload = {
        "primary_model": {"model_name": "gpt-x", "api_key": "sk-real-secret"},
        "database": {"host": "localhost", "password": ""},
        "mcp": {"servers": {"amap": {"env": {"AMAP_TOKEN": "abc"}}}},
        "rag": {"top_k": 5},
    }
    redacted = redact(payload)

    assert redacted["primary_model"]["model_name"] == "gpt-x"
    assert redacted["primary_model"]["api_key"] == "<set>"
    assert redacted["database"]["host"] == "localhost"
    assert redacted["database"]["password"] == "<unset>"
    assert redacted["mcp"]["servers"]["amap"]["env"]["AMAP_TOKEN"] == "<set>"
    assert redacted["rag"]["top_k"] == 5
    assert "sk-real-secret" not in json.dumps(redacted)

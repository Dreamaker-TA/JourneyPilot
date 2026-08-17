"""备份：产出一个**可验证**的目录，而不是一个 dump 文件。

dev docs 02 §5 的核心一句话：「只看到 `pg_dump` exit code 0 还不够」。一个截断的
dump 文件同样以 0 退出，而它会在真正需要恢复的那一天才暴露。所以每次备份都做三件
校验：文件非空、`pg_restore --list` 能解析、SHA-256 写进 manifest。

目录长这样（§5.1）：

```
backups/backup-20260817T101530Z-preupgrade/
  manifest.json            ← 唯一的元数据真相
  database.dump            ← pg_dump --format=custom
  schema_fingerprint.json  ← 恢复后可比对结构
  config.redacted.yaml     ← 结构保留，Secret 只记 <set> / <unset>
  checksums.sha256         ← 常规工具（sha256sum -c）也能校验
```

**不进备份**：API key 明文、HuggingFace 模型缓存、npm/uv 缓存、可重新下载的静态依赖、
临时日志。它们体积大、可重建，而第一项还会把 Secret 复制到一个用户随手拷来拷去的目录里。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .census import take_census
from .connection import DatabaseTarget
from .fingerprint import fingerprint_digest, fingerprint_sync
from .pg_tools import PgToolRunner, resolve_runner

logger = logging.getLogger(__name__)

#: manifest 结构版本。restore 用它判断「这个备份这份代码认不认识」。
BACKUP_FORMAT_VERSION = 1

DUMP_FILENAME = "database.dump"
MANIFEST_FILENAME = "manifest.json"
FINGERPRINT_FILENAME = "schema_fingerprint.json"
REDACTED_CONFIG_FILENAME = "config.redacted.yaml"
CHECKSUMS_FILENAME = "checksums.sha256"

#: 自动备份保留份数（§5.4）。手工备份永不自动删除。
DEFAULT_KEEP_AUTOMATIC = 5



class BackupError(RuntimeError):
    """备份没有成功产出一份可验证的快照。**调用方绝不能继续迁移。**"""


class BackupVerificationError(BackupError):
    """dump 产出了，但校验没过 —— 比没有备份更危险，必须当失败处理。"""


@dataclass(frozen=True)
class BackupResult:
    directory: Path
    manifest: dict[str, Any]

    @property
    def dump_path(self) -> Path:
        return self.directory / DUMP_FILENAME

    @property
    def bytes_written(self) -> int:
        return int(self.manifest.get("total_bytes", 0))


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _application_version() -> str:
    """从 pyproject.toml 读版本号。**不在代码里再抄一份。**"""

    import tomllib

    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    try:
        with pyproject.open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except Exception:  # pragma: no cover — 只在仓库结构被破坏时
        return "unknown"


def _write_redacted_config(directory: Path) -> str | None:
    """把 config.yaml 结构保留、Secret 脱敏后存进备份目录。

    脱敏规则用 `config/redaction.py` 那一份，不在这里再写一遍：同一件事两套写法会
    给出两组不同的标记字符串（``<configured>`` 与 ``<set>``），而读的人无法判断
    它们是不是同一个意思。
    """

    import yaml

    from ..config import find_config_yaml, load_yaml, redact

    path = find_config_yaml()
    if path is None:
        return None
    data = redact(load_yaml(path))
    target = directory / REDACTED_CONFIG_FILENAME
    target.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8"
    )
    return str(path)


def _database_size_bytes(conn: Any, database: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(%s)", (database,))
        return int(cur.fetchone()[0])


def _check_disk_space(directory: Path, needed_bytes: int) -> None:
    """空间不够就**拒绝**，而不是让 dump 写到一半再失败（§13.1）。

    阈值取「未压缩库大小 + 20% 余量」。custom format 是压缩的，所以这是保守估计 ——
    宁可多要一点空间，也不要在升级前那一刻才发现盘满了。
    """

    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free
    required = int(needed_bytes * 1.2)
    if free < required:
        raise BackupError(
            f"磁盘空间不足，拒绝备份：{directory} 可用 {free / 1e9:.2f} GB，"
            f"数据库约 {needed_bytes / 1e9:.2f} GB（含 20% 余量需要 {required / 1e9:.2f} GB）。"
            "清理空间或用 --output 指定另一个磁盘后重试。"
        )


def _verify_dump(runner: PgToolRunner, target: DatabaseTarget, dump_path: Path) -> int:
    """三条校验里的前两条：文件非空 + `pg_restore --list` 能解析。"""

    if not dump_path.exists():
        raise BackupVerificationError(f"pg_dump 没有产出文件：{dump_path}")
    size = dump_path.stat().st_size
    if size == 0:
        raise BackupVerificationError(f"pg_dump 产出的是空文件：{dump_path}")

    result = runner.run(
        "pg_restore", ["--list"], target=target, stdin_path=dump_path, timeout=600
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise BackupVerificationError(
            f"备份文件无法被 pg_restore 解析（很可能被截断）：{dump_path}\n{stderr}"
        )
    entries = (result.stdout or b"").decode("utf-8", "replace")
    if "TOC" not in entries and not entries.strip():
        raise BackupVerificationError(f"备份文件的 TOC 为空：{dump_path}")
    return size


def create_backup(
    conn: Any,
    target: DatabaseTarget,
    *,
    embedding_dimensions: int,
    output_root: Path,
    kind: str = "manual",
    label: str = "",
    preferred_container: str = "",
) -> BackupResult:
    """产出一份经过校验的备份。任何一步失败都抛异常，绝不留下「看起来成功」的目录。

    `conn` 是一个已连到 `target` 的 psycopg3 连接，只用来读 census / 指纹 / 库大小；
    实际导出走 `pg_dump`。
    """

    census = take_census(conn)
    if not census.reachable:  # pragma: no cover — take_census 连不上时直接抛
        raise BackupError("数据库不可达，无法备份")

    runner = resolve_runner(
        target,
        server_major=census.postgres_major,
        preferred_container=preferred_container,
    )
    fingerprint = fingerprint_sync(conn, embedding_dimensions=embedding_dimensions)
    size_estimate = _database_size_bytes(conn, target.database)

    suffix = f"-{label}" if label else ""
    directory = output_root / f"backup-{_utc_stamp()}{suffix}"
    if directory.exists():
        raise BackupError(f"备份目录已存在，拒绝覆盖：{directory}")
    _check_disk_space(output_root, size_estimate)
    directory.mkdir(parents=True)

    logger.info(
        "开始备份 %s → %s（%s，库约 %.1f MB）",
        target.describe(), directory, runner.describe(), size_estimate / 1e6,
    )

    dump_path = directory / DUMP_FILENAME
    result = runner.run(
        "pg_dump",
        ["--format=custom", "--no-owner", "--no-acl", "--dbname", target.database],
        target=target,
        stdout_path=dump_path,
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
        shutil.rmtree(directory, ignore_errors=True)
        raise BackupError(f"pg_dump 失败（exit {result.returncode}）：\n{stderr}")

    dump_bytes = _verify_dump(runner, target, dump_path)

    (directory / FINGERPRINT_FILENAME).write_text(
        json.dumps(fingerprint, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    config_source = _write_redacted_config(directory)

    files: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir()):
        if path.name == MANIFEST_FILENAME:
            continue
        files.append(
            {"name": path.name, "sha256": _sha256(path), "bytes": path.stat().st_size}
        )

    manifest: dict[str, Any] = {
        "backup_format_version": BACKUP_FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "label": label,
        "application_version": _application_version(),
        "database": {
            "name": target.database,
            "endpoint": target.describe(),
            "postgres_version": census.postgres_version,
            "postgres_major": census.postgres_major,
        },
        "migration_revision": census.alembic_revision,
        "schema_fingerprint_sha256": fingerprint_digest(fingerprint),
        "row_counts": dict(census.row_counts),
        "total_business_rows": census.total_business_rows,
        "optional_capabilities": census.optional_capabilities,
        "embedding_columns": fingerprint.get("embedding_columns", {}),
        "pg_dump": {
            "strategy": runner.strategy,
            "client_major": runner.client_major,
            "container": runner.container,
            "format": "custom",
            "bytes": dump_bytes,
        },
        "config_source": config_source,
        "files": files,
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "verified": {
            "dump_non_empty": True,
            "pg_restore_list_parsed": True,
            "checksums_written": True,
        },
    }
    (directory / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # 让 `sha256sum -c checksums.sha256` 这种常规工具也能校验，不必先读懂 manifest。
    (directory / CHECKSUMS_FILENAME).write_text(
        "".join(f"{item['sha256']}  {item['name']}\n" for item in files), encoding="utf-8"
    )

    logger.info(
        "备份完成并校验通过：%s（%.1f MB，revision %s）",
        directory, manifest["total_bytes"] / 1e6, census.alembic_revision,
    )
    return BackupResult(directory=directory, manifest=manifest)


def load_manifest(directory: Path) -> dict[str, Any]:
    path = directory / MANIFEST_FILENAME
    if not path.exists():
        raise BackupError(f"不是一个 JourneyPilot 备份目录（缺 {MANIFEST_FILENAME}）：{directory}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_backup(directory: Path) -> list[str]:
    """离线校验一个备份目录。返回问题清单，空 = 通过。

    校验 checksum 而不只是「文件在不在」：备份最常见的坏法是拷贝过程中被截断，
    而截断后的文件仍然存在。
    """

    problems: list[str] = []
    try:
        manifest = load_manifest(directory)
    except BackupError as exc:
        return [str(exc)]

    version = manifest.get("backup_format_version")
    if version != BACKUP_FORMAT_VERSION:
        problems.append(
            f"备份格式版本 {version} 与这份代码支持的 {BACKUP_FORMAT_VERSION} 不同"
        )

    for item in manifest.get("files", []):
        path = directory / item["name"]
        if not path.exists():
            problems.append(f"缺文件：{item['name']}")
            continue
        actual_size = path.stat().st_size
        if actual_size != item["bytes"]:
            problems.append(
                f"{item['name']} 大小不符：manifest {item['bytes']}，实际 {actual_size}"
            )
            continue
        actual_digest = _sha256(path)
        if actual_digest != item["sha256"]:
            problems.append(
                f"{item['name']} SHA-256 不符：manifest {item['sha256'][:16]}…，"
                f"实际 {actual_digest[:16]}…"
            )

    if not (directory / DUMP_FILENAME).exists():
        problems.append(f"缺数据库转储文件 {DUMP_FILENAME}")

    return problems


def list_backups(output_root: Path) -> list[dict[str, Any]]:
    """按时间倒序列出备份。读不懂 manifest 的目录也列出来并标注，不静默跳过。"""

    if not output_root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for directory in sorted(output_root.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        try:
            manifest = load_manifest(directory)
        except BackupError as exc:
            entries.append({"path": str(directory), "readable": False, "problem": str(exc)})
            continue
        entries.append(
            {
                "path": str(directory),
                "readable": True,
                "created_at": manifest.get("created_at"),
                "kind": manifest.get("kind"),
                "label": manifest.get("label"),
                "migration_revision": manifest.get("migration_revision"),
                "total_bytes": manifest.get("total_bytes"),
                "application_version": manifest.get("application_version"),
            }
        )
    return entries


def prune_automatic_backups(
    output_root: Path, *, keep: int = DEFAULT_KEEP_AUTOMATIC, dry_run: bool = False
) -> list[str]:
    """只删自动备份，只删超出保留份数的那些。返回被删（或将被删）的目录。

    **手工备份永不自动删除**（§5.4）：用户敲过 `journeypilot backup` 就意味着
    「这一份我要留着」，保留策略无权推翻它。读不懂 manifest 的目录同样不删 ——
    分不清它是什么的时候，删除是最坏的选择。
    """

    automatic = [
        entry
        for entry in list_backups(output_root)
        if entry.get("readable") and entry.get("kind") == "automatic"
    ]
    doomed = [entry["path"] for entry in automatic[keep:]]
    if not dry_run:
        for path in doomed:
            shutil.rmtree(path, ignore_errors=True)
            logger.info("已清理超出保留份数的自动备份：%s", path)
    return doomed


def restore_list(
    directory: Path, target: DatabaseTarget, *, server_major: int, preferred_container: str = ""
) -> str:
    """`pg_restore --list` 的输出，给 doctor / 人工检查用。"""

    runner = resolve_runner(
        target, server_major=server_major, preferred_container=preferred_container
    )
    result: subprocess.CompletedProcess = runner.run(
        "pg_restore", ["--list"], target=target, stdin_path=directory / DUMP_FILENAME
    )
    return (result.stdout or b"").decode("utf-8", "replace")

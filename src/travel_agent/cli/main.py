"""`journeypilot` CLI：doctor / migrate / backup / restore。

这是 dev docs ADR-P0-03 里那个「启动编排器」。它是启动路径的一部分，不是可选的运维工具：
API 进程不建表，所以 `migrate` 必须在 API 之前跑完（Compose entrypoint 与 `run.sh` 都这么做）。

```
journeypilot CLI                     API 进程
  ├─ census                            ├─ connect
  ├─ backup                            ├─ 只读合同校验
  ├─ migrate（持锁；含 checkpoint 表）  └─ serve or readiness 503
  ├─ restore
  └─ doctor
```

**每个子命令都有 `--json`**：文本给人看并给出下一条可执行命令，JSON 给 CI 和自动化
（dev docs 02 §12）。退出码：0 成功 / 1 失败或被拒绝 / 2 用法错误（argparse 的约定）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("journeypilot")

_REPO_ROOT = Path(__file__).resolve().parents[3]

EXIT_OK = 0
EXIT_FAILED = 1


# --------------------------------------------------------------------------- #
# 公共
# --------------------------------------------------------------------------- #


def _settings() -> Any:
    from ..config import get_settings

    return get_settings()


def _target(settings: Any, database: str | None = None) -> Any:
    from ..db.connection import DatabaseTarget

    return DatabaseTarget.from_settings(settings, database=database)


def _backup_root(settings: Any, override: str | None = None) -> Path:
    """备份根目录。相对路径按**仓库根**解析，不按当前工作目录。

    从哪个目录敲命令不该改变备份落在哪里 —— 否则「我明明备份过」会变成
    「备份散落在三个目录里，没人知道最新的那份在哪」。
    """

    raw = override or settings.maintenance.backup_dir
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (_REPO_ROOT / path)


def _emit(payload: dict[str, Any], *, as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for line in lines:
            print(line)


def _plan_lines(plan: Any) -> list[str]:
    from ..db.migrate import Decision

    census = plan.census
    lines = [
        f"数据库          {census.postgres_version.split(' on ')[0]}",
        f"迁移版本        {plan.current_revision or '（未纳管）'}  →  head {plan.head_revision}",
        f"结构指纹        {plan.fingerprint_digest[:16]}…",
        f"业务行数        {census.total_business_rows}",
        f"核心扩展        {'、'.join(census.installed_extensions) or '（无）'}",
    ]
    missing_ext = census.missing_required_extensions
    if missing_ext:
        lines.append(f"缺核心扩展      {'、'.join(missing_ext)}")
    capabilities = ", ".join(
        f"{name}={'on' if enabled else 'off'}"
        for name, enabled in census.optional_capabilities.items()
    )
    lines.append(f"可选能力        {capabilities or '（无）'}")
    if census.unmanaged_tables:
        lines.append(f"合同外的表      {'、'.join(census.unmanaged_tables)}")

    verdict = {
        Decision.MIGRATE_EMPTY: "空库：可以直接迁移（不需要备份）",
        Decision.ADOPT_BASELINE: "结构等于 baseline：将纳管（stamp）而不重复建表",
        Decision.UP_TO_DATE: "已是最新，无需迁移",
        Decision.UPGRADE: "落后于 head：迁移前会自动备份",
        Decision.REFUSE_UNKNOWN_SCHEMA: "拒绝：结构来历不明",
        Decision.REFUSE_UNKNOWN_REVISION: "拒绝：数据库的 revision 这份代码不认识",
        Decision.REFUSE_SCHEMA_AHEAD: "拒绝：数据库比代码新",
        Decision.REFUSE_NEEDS_DESTRUCTIVE_CONSENT: "拒绝：需要显式授权破坏性迁移",
        Decision.REFUSE_UNREACHABLE: "拒绝：数据库不可达",
    }[plan.decision]
    lines.append("")
    lines.append(f"判定            {plan.decision.value} —— {verdict}")

    if plan.pending_revisions:
        lines.append(f"待执行迁移      {'、'.join(plan.pending_revisions)}")
    if plan.destructive_revisions:
        lines.append(f"其中破坏性的    {'、'.join(plan.destructive_revisions)}")
    for note in plan.notes:
        lines.append(f"说明            {note}")
    for problem in plan.problems:
        lines.append(f"问题            {problem}")
    if plan.next_action:
        lines.append(f"下一步          {plan.next_action}")
    return lines


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def _background_job_backlog(conn: Any) -> dict[str, Any]:
    """后台任务的积压与死信。表还没建出来时如实说读不到，不算作 0。"""

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM background_jobs GROUP BY status")
            counts = {str(status): int(total) for status, total in cur.fetchall()}
    except Exception as exc:
        conn.rollback()
        first_line = str(exc).splitlines()[0] if str(exc) else ""
        return {"readable": False, "problem": f"{type(exc).__name__}: {first_line}"}
    return {
        "readable": True,
        "pending": counts.get("pending", 0),
        "retry_wait": counts.get("retry_wait", 0),
        "running": counts.get("running", 0),
        "dead": counts.get("dead", 0),
        "completed": counts.get("completed", 0),
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    from ..db.backup import list_backups
    from ..db.checkpoint_schema import missing_checkpoint_tables
    from ..db.connection import connect
    from ..db.lock import release, try_acquire
    from ..db.migrate import plan

    settings = _settings()
    target = _target(settings)
    payload: dict[str, Any] = {"endpoint": target.describe()}
    lines: list[str] = [f"连接            {target.describe()}"]

    try:
        with connect(target) as conn:
            migration_plan = plan(
                conn,
                target,
                embedding_dimensions=settings.embedding.dimensions,
                allow_destructive=False,
            )
            missing_checkpoints = missing_checkpoint_tables(conn)
            job_backlog = _background_job_backlog(conn)
            # 「现在有人在迁移吗」：拿到锁就立刻还回去，doctor 不持锁。
            lock_free = try_acquire(conn)
            if lock_free:
                release(conn)
    except Exception as exc:
        payload["database"] = {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
        _emit(
            payload,
            as_json=args.json,
            lines=lines + [f"数据库          不可达：{exc}", "", "下一步          确认 PostgreSQL 已启动、config.yaml 里的地址与口令正确"],
        )
        return EXIT_FAILED

    census = migration_plan.census
    payload["database"] = {
        "reachable": True,
        "postgres_version": census.postgres_version,
        "postgres_major": census.postgres_major,
        "migration_revision": migration_plan.current_revision,
        "head_revision": migration_plan.head_revision,
        "schema_compatible": not migration_plan.refused,
        "backup_recommended": migration_plan.backup_required,
        "migration_lock_free": lock_free,
    }
    payload["extensions"] = {
        name: name in census.installed_extensions
        for name in (*census.installed_extensions, *census.missing_required_extensions)
    }
    payload["optional_capabilities"] = census.optional_capabilities
    payload["migration"] = migration_plan.to_dict()
    payload["census"] = census.to_dict()
    # checkpoint 表不在 census / 指纹里（owner 是 langgraph），但缺了它们
    # plan_gate 与崩溃恢复就是关着的。
    payload["checkpoint_schema"] = {
        "ready": not missing_checkpoints,
        "missing_tables": list(missing_checkpoints),
        "next_action": "journeypilot migrate" if missing_checkpoints else "",
    }

    payload["background_jobs"] = job_backlog

    backup_root = _backup_root(settings, args.backup_dir)
    backups = list_backups(backup_root)
    payload["backups"] = {"root": str(backup_root), "count": len(backups), "entries": backups[:5]}

    # embedding 合同的完整校验是 P0-D（PR-P0-07）的事。这里先如实报告
    # 「配置说多少维、实际列是多少维」——不做兼容性结论，因为同维度不同模型
    # 也不兼容，而模型身份此刻还没有被持久化。
    payload["embedding"] = {
        "configured": {
            "provider": settings.embedding.provider,
            "model": settings.embedding.model_name,
            "dimensions": settings.embedding.dimensions,
        },
        "columns": migration_plan.fingerprint.get("embedding_columns", {}),
        "stored": None,
        "note": "provider/model 尚未持久化，维度一致不等于向量空间兼容（P0-D 补齐）",
    }

    if not args.json:
        lines.extend(_plan_lines(migration_plan))
        lines.append("")
        lines.append(
            "checkpoint 表   "
            + ("就绪" if not missing_checkpoints else f"缺 {'、'.join(missing_checkpoints)} —— journeypilot migrate")
        )
        lines.append(
            "后台任务        "
            + (
                f"待处理 {job_backlog['pending']}、重试等待 {job_backlog['retry_wait']}、"
                f"死信 {job_backlog['dead']}"
                if job_backlog.get("readable")
                else f"读不到：{job_backlog.get('problem')}"
            )
        )
        lines.append(f"迁移锁          {'空闲' if lock_free else '被占用（另一个 migrate 在跑）'}")
        lines.append(f"备份目录        {backup_root}（{len(backups)} 份）")
        for entry in backups[:5]:
            if entry.get("readable"):
                lines.append(
                    f"  - {Path(entry['path']).name}  {entry.get('kind')}  "
                    f"revision={entry.get('migration_revision')}  "
                    f"{(entry.get('total_bytes') or 0) / 1e6:.1f} MB"
                )
            else:
                lines.append(f"  - {Path(entry['path']).name}  ⚠ {entry.get('problem')}")

    _emit(payload, as_json=args.json, lines=lines)
    return EXIT_FAILED if migration_plan.refused else EXIT_OK


# --------------------------------------------------------------------------- #
# migrate
# --------------------------------------------------------------------------- #


def cmd_migrate(args: argparse.Namespace) -> int:
    from ..db.checkpoint_schema import create_checkpoint_schema
    from ..db.connection import connect
    from ..db.migrate import Decision, plan, upgrade_sql

    settings = _settings()
    target = _target(settings)

    if args.sql:
        # 离线输出 SQL：不连库、不加锁、不改任何东西。
        print(
            "# 离线预览：以下 SQL 未经任何数据库探测生成，也没有经过 census、"
            "备份和破坏性授权这几道闸门。\n"
            "# 它用来回答「迁移要做什么」，不是用来手工执行的部署脚本 —— "
            "真正的升级请用 `journeypilot migrate`。",
            file=sys.stderr,
        )
        upgrade_sql(target)
        return EXIT_OK

    try:
        with connect(target) as conn:
            migration_plan = plan(
                conn,
                target,
                embedding_dimensions=settings.embedding.dimensions,
                allow_destructive=args.allow_destructive,
            )
    except Exception as exc:
        print(f"数据库不可达：{exc}", file=sys.stderr)
        return EXIT_FAILED

    for line in _plan_lines(migration_plan):
        print(line)

    if migration_plan.refused:
        print("", file=sys.stderr)
        print("拒绝执行迁移。上面的「问题」和「下一步」说明了原因。", file=sys.stderr)
        return EXIT_FAILED

    if args.dry_run:
        print("\n--dry-run：以上是将要执行的动作，未改动数据库。")
        return EXIT_OK

    if migration_plan.decision is not Decision.UP_TO_DATE:
        code = _run_migration(args, settings, target, migration_plan)
        if code != EXIT_OK:
            return code

    # ---- LangGraph checkpoint 表 ------------------------------------------ #
    # 即使自己的迁移无事可做也要跑：这四张表不在 `alembic_version` 的管辖里，
    # 「已是最新」不代表它们建好了。
    try:
        created = create_checkpoint_schema(target)
    except Exception as exc:
        print(f"\nLangGraph checkpoint 表建立失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("没有它们，plan_gate 与崩溃恢复不可用（API 会报 checkpointer 不可用）。", file=sys.stderr)
        return EXIT_FAILED
    if created:
        print(f"\n已建立 LangGraph checkpoint 表：{'、'.join(created)}")

    with connect(target) as conn:
        after = plan(conn, target, embedding_dimensions=settings.embedding.dimensions)
    print("")
    print(f"迁移完成：revision {after.current_revision}，指纹 {after.fingerprint_digest[:16]}…")
    return EXIT_OK


def _run_migration(
    args: argparse.Namespace, settings: Any, target: Any, migration_plan: Any
) -> int:
    """备份闸门 + 持锁执行。已经判定过不拒绝、不是 dry-run、且确有待执行迁移。"""

    from ..db.backup import create_backup
    from ..db.connection import connect
    from ..db.lock import MigrationLockTimeout, migration_lock
    from ..db.migrate import Decision, plan, stamp, upgrade

    # ---- 备份闸门 --------------------------------------------------------- #
    # 非空库必须先有一份**校验通过**的备份才允许自动迁移（dev docs 02 §4.3）。
    # `--skip-backup` 存在是因为测试和一次性临时库需要它，但它会打印警告：
    # 用户在生产数据上敲它，应该知道自己关掉了什么。
    if migration_plan.backup_required and not args.skip_backup:
        print("")
        print("非空数据库：迁移前自动备份…")
        try:
            with connect(target) as conn:
                result = create_backup(
                    conn,
                    target,
                    embedding_dimensions=settings.embedding.dimensions,
                    output_root=_backup_root(settings, args.backup_dir),
                    kind="automatic",
                    label="preupgrade",
                    preferred_container=settings.maintenance.postgres_container,
                )
        except Exception as exc:
            print(f"\n备份失败，因此不执行迁移：{exc}", file=sys.stderr)
            return EXIT_FAILED
        print(f"备份完成并校验通过：{result.directory}（{result.bytes_written / 1e6:.1f} MB）")
    elif migration_plan.backup_required:
        print("")
        print("⚠ --skip-backup：跳过升级前备份。迁移失败时没有可恢复的快照。")

    # ---- 持锁执行 --------------------------------------------------------- #
    try:
        with connect(target) as conn:
            with migration_lock(
                conn, timeout_seconds=settings.maintenance.migration_lock_timeout_seconds
            ):
                # 拿到锁之后**重新判定**：等锁的这段时间里另一个 migrator 可能已经
                # 把库迁完了。拿旧结论继续 = 对着一个已经变了的库执行计划。
                fresh = plan(
                    conn,
                    target,
                    embedding_dimensions=settings.embedding.dimensions,
                    allow_destructive=args.allow_destructive,
                )
                if fresh.refused:
                    print(
                        "\n拿到迁移锁后重新判定为拒绝："
                        + "；".join(fresh.problems),
                        file=sys.stderr,
                    )
                    return EXIT_FAILED
                if fresh.decision is Decision.UP_TO_DATE:
                    print("\n另一个进程已经把数据库迁到最新，无需重复执行。")
                    return EXIT_OK
                if fresh.decision is Decision.ADOPT_BASELINE:
                    # 纳管和后续迁移是两件事：先把版本号写上（不改数据），
                    # 再重新判定还剩什么。这样「纳管成功但后面需要 --allow-destructive」
                    # 不会退化成「什么都没做」。
                    stamp(target, _baseline_revision())
                    print(f"\n已纳管：{target.database} → {_baseline_revision()}（未执行 DDL）")
                    after_adopt = plan(
                        conn,
                        target,
                        embedding_dimensions=settings.embedding.dimensions,
                        allow_destructive=args.allow_destructive,
                    )
                    if after_adopt.decision is Decision.UP_TO_DATE:
                        print("数据库已是最新。")
                        return EXIT_OK
                    if after_adopt.refused:
                        print("", file=sys.stderr)
                        for problem in after_adopt.problems:
                            print(f"问题            {problem}", file=sys.stderr)
                        print(
                            f"下一步          {after_adopt.next_action}", file=sys.stderr
                        )
                        return EXIT_FAILED
                upgrade(target)
    except MigrationLockTimeout as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:
        print(f"\n迁移失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("数据库已回滚到这一条迁移执行前的状态（PostgreSQL 事务性 DDL）。", file=sys.stderr)
        return EXIT_FAILED

    return EXIT_OK


def _baseline_revision() -> str:
    from ..db.schema_contract import BASELINE_REVISION

    return BASELINE_REVISION


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #


def cmd_backup(args: argparse.Namespace) -> int:
    from ..db.backup import create_backup, list_backups, prune_automatic_backups, verify_backup
    from ..db.connection import connect

    settings = _settings()
    target = _target(settings)
    root = _backup_root(settings, args.backup_dir)

    if args.list:
        entries = list_backups(root)
        _emit(
            {"root": str(root), "entries": entries},
            as_json=args.json,
            lines=[f"备份目录 {root}（{len(entries)} 份）"]
            + [
                (
                    f"  {Path(e['path']).name}  {e.get('kind')}  "
                    f"revision={e.get('migration_revision')}  "
                    f"{(e.get('total_bytes') or 0) / 1e6:.1f} MB  {e.get('created_at')}"
                )
                if e.get("readable")
                else f"  {Path(e['path']).name}  ⚠ {e.get('problem')}"
                for e in entries
            ],
        )
        return EXIT_OK

    if args.verify:
        problems = verify_backup(Path(args.verify))
        _emit(
            {"path": args.verify, "ok": not problems, "problems": problems},
            as_json=args.json,
            lines=[f"校验 {args.verify}：{'通过' if not problems else '未通过'}"]
            + [f"  - {p}" for p in problems],
        )
        return EXIT_OK if not problems else EXIT_FAILED

    if args.prune:
        doomed = prune_automatic_backups(
            root, keep=settings.maintenance.keep_automatic_backups, dry_run=args.dry_run
        )
        _emit(
            {"pruned": doomed, "dry_run": args.dry_run},
            as_json=args.json,
            lines=[
                f"{'将删除' if args.dry_run else '已删除'} {len(doomed)} 份超出保留数"
                f"（{settings.maintenance.keep_automatic_backups}）的自动备份"
            ]
            + [f"  - {p}" for p in doomed]
            + ["手工备份不在清理范围内。"],
        )
        return EXIT_OK

    try:
        with connect(target) as conn:
            result = create_backup(
                conn,
                target,
                embedding_dimensions=settings.embedding.dimensions,
                output_root=root,
                kind=args.kind,
                label=args.label,
                preferred_container=settings.maintenance.postgres_container,
            )
    except Exception as exc:
        print(f"备份失败：{exc}", file=sys.stderr)
        return EXIT_FAILED

    _emit(
        {"directory": str(result.directory), "manifest": result.manifest},
        as_json=args.json,
        lines=[
            f"备份完成并校验通过：{result.directory}",
            f"  转储       {result.manifest['pg_dump']['bytes'] / 1e6:.1f} MB"
            f"（{result.manifest['pg_dump']['strategy']}，客户端 "
            f"PostgreSQL {result.manifest['pg_dump']['client_major']}）",
            f"  revision   {result.manifest['migration_revision']}",
            f"  业务行数   {result.manifest['total_business_rows']}",
            "  校验       文件非空 ✓  pg_restore --list 可解析 ✓  SHA-256 已记录 ✓",
        ],
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #


def cmd_restore(args: argparse.Namespace) -> int:
    from ..db.restore import RestoreError, drop_retained, list_retained, restore

    settings = _settings()
    target = _target(settings)

    if args.list_retained:
        retained = list_retained(target)
        _emit(
            {"retained": retained},
            as_json=args.json,
            lines=[f"恢复留档库（{len(retained)} 个）"] + [f"  - {name}" for name in retained],
        )
        return EXIT_OK

    if args.drop_retained:
        try:
            drop_retained(target, args.drop_retained)
        except RestoreError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_FAILED
        print(f"已删除 {args.drop_retained}")
        return EXIT_OK

    if not args.directory:
        print("需要指定备份目录，或使用 --list-retained / --drop-retained", file=sys.stderr)
        return EXIT_FAILED

    if not args.yes:
        print(
            f"即将把 {args.directory} 恢复成数据库 {target.describe()}。\n"
            "当前数据库会被改名留档（不会删除），恢复失败时当前数据库不受影响。\n"
            "确认请加 --yes。",
            file=sys.stderr,
        )
        return EXIT_FAILED

    try:
        result = restore(
            target,
            Path(args.directory),
            embedding_dimensions=settings.embedding.dimensions,
            terminate_active=args.terminate_active,
            preferred_container=settings.maintenance.postgres_container,
        )
    except Exception as exc:
        print(f"恢复失败：{exc}", file=sys.stderr)
        return EXIT_FAILED

    _emit(
        result.to_dict(),
        as_json=args.json,
        lines=[
            f"恢复完成：{result.restored_database}",
            f"  备份时间       {result.manifest.get('created_at')}",
            f"  revision       {result.manifest.get('migration_revision')}",
            f"  结构校验       {'通过' if result.checks.get('schema_matches_backup') else '跳过/未通过'}",
            f"  行数校验       {'通过' if result.checks.get('row_counts_match') else '未通过'}",
            f"  留档旧库       {result.retained_database}",
            "",
            f"确认无误后删除留档：journeypilot restore --drop-retained {result.retained_database}",
        ],
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# db（开发者子命令）
# --------------------------------------------------------------------------- #


def cmd_db_status(args: argparse.Namespace) -> int:
    return cmd_doctor(args)


def cmd_db_write_fingerprint(args: argparse.Namespace) -> int:
    """把当前库的指纹写成某个 revision 的存档。**开发者命令。**

    存档是 adoption 判定和 API 只读校验的期望值，所以它只能从一个「刚刚由
    `alembic upgrade` 建出来的干净库」生成。这个命令因此要求库已被纳管、
    且 revision 与 `--revision` 一致 —— 否则会把一个漂移了的结构固化成期望值，
    从此所有校验都对着错的基准比。
    """

    from ..db.census import take_census
    from ..db.connection import connect
    from ..db.fingerprint import fingerprint_digest, fingerprint_sync, normative_only
    from ..db.migrate import FINGERPRINTS_DIR, revision_line

    settings = _settings()
    target = _target(settings, database=args.database)
    revision = args.revision

    line = revision_line()
    if revision not in line:
        print(f"revision {revision} 不在迁移历史里：{line}", file=sys.stderr)
        return EXIT_FAILED

    with connect(target) as conn:
        census = take_census(conn)
        if census.alembic_revision != revision:
            print(
                f"拒绝生成存档：{target.describe()} 的 revision 是 "
                f"{census.alembic_revision!r}，不是 {revision!r}。"
                "存档必须来自一个刚刚迁到该 revision 的干净库。",
                file=sys.stderr,
            )
            return EXIT_FAILED
        fingerprint = fingerprint_sync(
            conn, embedding_dimensions=settings.embedding.dimensions
        )

    FINGERPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    path = FINGERPRINTS_DIR / f"{revision}.json"
    archive = {
        "_note": (
            "由 `journeypilot db write-fingerprint` 生成。这是 revision "
            f"{revision} 的结构合同：adoption 判定与 API 只读校验都拿活库指纹与它比对。"
            "只包含进摘要的项 —— 装没装 zhparser、实际向量维度是环境事实，不在这里。"
            "手工编辑这个文件等于篡改校验基准。"
        ),
        **normative_only(fingerprint),
    }
    path.write_text(
        json.dumps(archive, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {path}（摘要 {fingerprint_digest(fingerprint)}）")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="journeypilot",
        description="JourneyPilot 维护命令：数据库迁移、备份与恢复、环境诊断。",
    )
    parser.add_argument(
        "--log-level", default="warning",
        help="CLI 自身的日志级别（默认 warning：命令输出本身就是结论）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--json", action="store_true", help="输出机器可读 JSON（供 CI 使用）")
        sub.add_argument(
            "--backup-dir", default=None,
            help="备份根目录（默认取 config.yaml 的 maintenance.backup_dir）",
        )

    doctor = subparsers.add_parser(
        "doctor", help="体检：数据库、迁移版本、结构、扩展、备份现状"
    )
    add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    migrate = subparsers.add_parser(
        "migrate",
        help="执行版本化迁移并建立 checkpoint 表（持锁；非空库先自动备份）。启动 API 之前必须先跑这条",
    )
    add_common(migrate)
    migrate.add_argument(
        "--allow-destructive", action="store_true",
        help="授权执行标记为破坏性（会丢数据或不可逆）的迁移",
    )
    migrate.add_argument(
        "--skip-backup", action="store_true",
        help="跳过升级前自动备份（危险：迁移失败时没有可恢复的快照）",
    )
    migrate.add_argument(
        "--dry-run", action="store_true", help="只打印判定与待执行迁移，不改数据库"
    )
    migrate.add_argument(
        "--sql", action="store_true",
        help="离线输出迁移 SQL 而不执行（不连库、不加锁；不含 langgraph 自己建的 checkpoint 表）",
    )
    migrate.set_defaults(func=cmd_migrate)

    backup = subparsers.add_parser("backup", help="生成一份经过校验的备份")
    add_common(backup)
    backup.add_argument("--label", default="", help="备份标签，写进目录名和 manifest")
    backup.add_argument(
        "--kind", choices=("manual", "automatic"), default="manual",
        help="manual（默认）永不被保留策略自动删除",
    )
    backup.add_argument("--list", action="store_true", help="列出已有备份")
    backup.add_argument("--verify", metavar="DIR", help="校验一个备份目录的 checksum")
    backup.add_argument(
        "--prune", action="store_true", help="清理超出保留份数的自动备份（不动手工备份）"
    )
    backup.add_argument("--dry-run", action="store_true", help="配合 --prune：只列出将删除的")
    backup.set_defaults(func=cmd_backup)

    restore = subparsers.add_parser(
        "restore", help="从备份恢复（先恢复到新库并验证，再切换；旧库改名留档）"
    )
    add_common(restore)
    restore.add_argument("directory", nargs="?", help="备份目录")
    restore.add_argument("--yes", action="store_true", help="确认执行恢复")
    restore.add_argument(
        "--terminate-active", action="store_true",
        help="切换前强制断开当前数据库上的活动连接",
    )
    restore.add_argument("--list-retained", action="store_true", help="列出恢复留档的旧库")
    restore.add_argument("--drop-retained", metavar="DBNAME", help="删除一个恢复留档库")
    restore.set_defaults(func=cmd_restore)

    db = subparsers.add_parser("db", help="开发者子命令")
    db_subparsers = db.add_subparsers(dest="db_command", required=True)

    db_status = db_subparsers.add_parser("status", help="等价于 doctor")
    add_common(db_status)
    db_status.set_defaults(func=cmd_db_status)

    db_fingerprint = db_subparsers.add_parser(
        "write-fingerprint",
        help="把当前库的结构指纹写成某个 revision 的存档（开发者）",
    )
    add_common(db_fingerprint)
    db_fingerprint.add_argument("--revision", required=True, help="目标 revision")
    db_fingerprint.add_argument(
        "--database", default=None, help="从哪个库读（默认 config.yaml 里的那个）"
    )
    db_fingerprint.set_defaults(func=cmd_db_write_fingerprint)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        format="%(levelname)s %(message)s",
    )
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

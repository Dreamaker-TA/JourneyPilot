# ADR-0002：版本化迁移，API 进程不改 Schema

状态：已采纳
影响：`migrations/`、`db/migrate.py`、`db/fingerprint.py`、`journeypilot` CLI、
容器 entrypoint、`run.sh`

## 决定

所有 DDL 只存在于 `migrations/versions/`，只由 `journeypilot migrate` 执行，并且
**在 API 启动之前**跑完。API 进程只做一次**只读**的结构合同校验（迁移 revision +
结构指纹），不通过就在 readiness 里报不就绪。

## 为什么

`CREATE TABLE IF NOT EXISTS` 那种自建表的写法有两个致命性质：它无法表达「改一列」，
而且**每个进程都是 DDL 的作者**。于是同一个库的结构取决于哪个版本的进程先启动过，
而没有任何一处记着它现在是什么形状。结构指纹存档存在的意义就是让「现在是什么形状」
有一个可比对的答案。

## 替代方案与为什么没选

- **进程启动时自动迁移**：升级与启动耦合，一次失败的迁移会变成一个重启循环，
  而诊断被滚掉。现在 entrypoint 迁移失败就**不启动 API**，理由留在日志里。
- **只靠 alembic revision 判断结构**：revision 只记「跑过哪些脚本」，记不了
  「有人手工改过一列」。指纹补的是这一维。

## 后果

- 新增迁移后必须重新生成指纹存档，否则若干 db 测试会红。
- 备份在升级前自动做（`--skip-backup` 要显式给）。

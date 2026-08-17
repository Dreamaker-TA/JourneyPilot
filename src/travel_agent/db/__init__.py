"""数据库生命周期能力：census、指纹、迁移锁、版本化迁移、备份与恢复。

**这个包与 `infrastructure/` 的分界是「谁有权改 Schema」**（dev docs ADR-P0-03）：
`infrastructure/` 里的 Store 只读写业务行，跑在 API 进程里；这个包里的东西改 Schema、
改物理数据库，只跑在 `journeypilot` CLI / 启动编排器里，是一个**独立进程**。

API 进程唯一被允许调用的模块是 `report`（纯只读）。其余模块都是同步的
（psycopg3），因为 Alembic 是同步的，而它们的调用方不是 ASGI 应用。
"""

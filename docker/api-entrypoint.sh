#!/bin/sh
# API 容器的启动编排器：先迁移，再 exec API。顺序不可交换 —— API 进程不建表。
# 迁移被拒绝时不 exec API，诊断留在 `docker compose logs api`（compose 里 api 的
# restart 因此不是 unless-stopped，否则拒绝会变成无限重启并滚掉诊断）。
set -e

echo "[entrypoint] 数据库迁移（API 之前）…"
if ! uv run python journeypilot.py migrate; then
    echo "[entrypoint] 迁移未通过，不启动 API。" >&2
    echo "[entrypoint] 诊断：docker compose run --rm api uv run python journeypilot.py doctor" >&2
    exit 1
fi

echo "[entrypoint] 启动 API…"
exec uv run python main.py

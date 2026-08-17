#!/bin/sh
# API 容器的启动编排器。**与 run.sh 走同一个 Python 编排器**（`journeypilot`
# CLI + main.py），两条路径的差别只在「前端从哪来、要不要 reload」，不在策略。
#
# 顺序不可交换：API 进程不建表（ADR-P0-03），所以迁移必须先跑完。
# 迁移被拒绝时不 exec API，诊断留在 `docker compose logs api`（compose 里 api 的
# restart 因此不是 unless-stopped，否则拒绝会变成无限重启并滚掉诊断）。
#
# 不用 `uv run`：镜像里 .venv 已在 PATH 上（Dockerfile 的 ENV），而 `uv run` 会去
# 校验并可能改写 lockfile —— 一个非 root 的运行层不该有写 lockfile 的权限，也不该
# 在每次启动时重新解析依赖。
set -e

echo "[entrypoint] 校验配置…"
if ! python journeypilot.py config validate; then
    echo "[entrypoint] 配置无效，不启动 API。" >&2
    exit 1
fi

echo "[entrypoint] 数据库迁移（API 之前）…"
if ! python journeypilot.py migrate; then
    echo "[entrypoint] 迁移未通过，不启动 API。" >&2
    echo "[entrypoint] 诊断：docker compose run --rm api python journeypilot.py doctor" >&2
    exit 1
fi

echo "[entrypoint] 启动 API…"
exec python main.py

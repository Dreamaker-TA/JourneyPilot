# JourneyPilot API 镜像。多阶段：编译工具留在 builder，运行层不带它们。
#
# 依赖分组由 build arg 选择（见 pyproject.toml 的 [dependency-groups]）：
#   docker build .                                       core + local-embedding（Compose 默认）
#   docker build --build-arg DEPENDENCY_GROUPS="" .       只有 core
#   docker build --build-arg DEPENDENCY_GROUPS="local-embedding cross-encoder" .
#
# 运行层以非 root 跑：这个进程接受用户上传并 fork 解析子进程，没有一步需要 root。

# ---------------------------------------------------------------------------
# builder：装依赖，带编译工具
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

# 固定 uv 版本：一个会自己升级的安装器让「同一个 commit 构出同一个镜像」不再成立。
ARG UV_VERSION=0.9.7
ARG DEPENDENCY_GROUPS="local-embedding"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

# 只 COPY 清单再装依赖：源码改动不该让依赖层失效。
COPY pyproject.toml uv.lock ./
# --frozen：lockfile 就是事实，构建期绝不允许它被重新解析。
RUN set -eu; \
    groups=""; \
    for group in ${DEPENDENCY_GROUPS}; do groups="${groups} --group ${group}"; done; \
    uv sync --frozen --no-dev ${groups}

# ---------------------------------------------------------------------------
# node-mcp：Node stdio MCP server 的包，装在自己的层
# ---------------------------------------------------------------------------
FROM node:22-slim AS node-mcp

WORKDIR /mcp
COPY package.json package-lock.json ./
# npm ci 而不是 npm install：后者会改写 lockfile，而一个构建期被改写却没被提交的
# lockfile 意味着镜像里的版本没有任何地方记着。
RUN npm ci --omit=dev

# ---------------------------------------------------------------------------
# runtime：不带编译工具，不以 root 跑
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# curl 供 healthcheck；libpq5 是 psycopg 的运行时（不是 libpq-dev）；
# nodejs 供 Node stdio MCP server（不带 npm：运行期不允许临时下载未锁定版本）。
#
# 字体：**fonts-wqy-microhei，不是 fonts-noto-cjk**。Debian 的 Noto CJK 打的是 CFF
# （PostScript）轮廓，reportlab 的 TTFont 解析器嵌不进去（实测
# `postscript outlines are not supported`），于是启动预检会判 PDF 导出不可用。
# WenQuanYi 是 TrueType 轮廓，是 `services/pdf_export.py` 候选表里的首选项。
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libpq5 \
        fonts-wqy-microhei \
        nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin --uid 10001 journeypilot

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=node-mcp /mcp/node_modules /app/node_modules
ENV PATH="/app/.venv/bin:${PATH}" \
    VIRTUAL_ENV="/app/.venv" \
    PYTHONUNBUFFERED=1

COPY src/ ./src/
COPY mcp_servers/ ./mcp_servers/
COPY configs/ ./configs/
COPY main.py journeypilot.py alembic.ini ./
# 迁移脚本与维护 CLI：entrypoint 要用它迁移，只读合同校验要读迁移历史，
# doctor/backup/restore 也是容器里唯一能敲的维护入口。
COPY migrations/ ./migrations/
COPY package.json package-lock.json ./
# **不复制 config.example.yaml 当 config.yaml**：一份示例伪装成真实配置，
# 会让「我的配置没生效」变成「我改的那份文件根本没被读」。Compose 把宿主机的
# config.yaml 挂进来；没挂也能跑（全部走 Pydantic 默认 + JOURNEYPILOT_* 环境变量）。

RUN mkdir -p outputs uploads logs backups \
    && chown -R journeypilot:journeypilot /app

COPY docker/api-entrypoint.sh /usr/local/bin/api-entrypoint.sh
RUN chmod +x /usr/local/bin/api-entrypoint.sh

USER journeypilot
ENTRYPOINT ["/usr/local/bin/api-entrypoint.sh"]

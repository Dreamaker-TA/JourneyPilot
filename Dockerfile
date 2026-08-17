FROM python:3.11-slim

WORKDIR /app

# 系统依赖 + uv（与本地 pyproject.toml / uv.lock 对齐）
RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    libpq-dev \
    fonts-noto-cjk \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Python 依赖（与 pyproject.toml / uv.lock 对齐）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# 复制源代码
COPY src/ ./src/
COPY mcp_servers/ ./mcp_servers/
COPY main.py .
# 迁移脚本与维护 CLI：entrypoint 要用它迁移，只读合同校验要读迁移历史，
# doctor/backup/restore 也是容器里唯一能敲的维护入口。
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY journeypilot.py .
COPY config.example.yaml ./config.yaml
COPY package.json ./
COPY package-lock.json ./

# MCP Node 依赖
RUN npm install

# 创建必要目录
RUN mkdir -p outputs uploads logs

# 启动编排器 → API
COPY docker/api-entrypoint.sh /usr/local/bin/api-entrypoint.sh
RUN chmod +x /usr/local/bin/api-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/api-entrypoint.sh"]

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
# 迁移脚本与维护 CLI。镜像里必须有它们，原因有两个：
#  1) 容器内的只读 schema 报告要读迁移历史才能说出「代码的 head 是哪一个」；
#  2) `docker compose exec api uv run python journeypilot.py doctor/backup/migrate`
#     是容器化部署里唯一能敲的维护入口 —— 宿主机上可能连 PostgreSQL 客户端都没有。
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

# 启动
CMD ["uv", "run", "python", "main.py"]

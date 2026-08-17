# PostgreSQL + pgvector + zhparser（**可选** profile，见 docker-compose.yml）。
#
# 默认档用官方 pgvector 镜像，不编译任何东西。这一份只在用户显式选择中文分词增强时
# 构建：`docker compose --profile zhparser up -d --build postgres-zhparser redis api`。
#
# 供应链要求（dev_docs 03 §5.4）：
#   - 基础镜像固定 digest（多架构 index，含 amd64 与 arm64）；
#   - **HTTPS + 正常签名校验**，不用 `[trusted=yes]` 关掉验签。原来这里为了绕开
#     一次网络问题换了 HTTP 镜像源并加了 trusted=yes —— 那等于让任何能改写这条
#     链路的人往数据库镜像里塞东西，而它换来的只是几分钟的构建时间；
#   - 源码 tarball 固定 URL + SHA-256。
#
# 网络不通时**让它失败**：一个装不上 zhparser 的环境应当拿到一条构建错误，而不是
# 一个悄悄降级验签的镜像。默认档不受影响 —— 那正是把它拆成 profile 的理由。
FROM pgvector/pgvector:pg18@sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a

# 编译工具 + PostgreSQL 18 开发头文件。走基础镜像自带的 HTTPS Debian/PGDG 源与
# 它们自己的签名密钥，不改 sources.list。
RUN printf 'Acquire::Retries "5";\nAcquire::http::Timeout "60";\n' \
        > /etc/apt/apt.conf.d/99retries \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        postgresql-server-dev-18 \
        libpcre3-dev \
        autoconf \
        automake \
        libtool \
        wget \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# SCWS + zhparser：固定 release tarball 与 sha256，新环境可直接构建，
# 不依赖仓库里额外提交二进制源码包。
WORKDIR /tmp
ARG SCWS_URL="https://github.com/hightman/scws/archive/refs/tags/1.2.3.tar.gz"
ARG SCWS_SHA256="98e5932221029f507eadc762c4036eaf95f31f62f45f972401ec6952258feb10"
ARG ZHPARSER_URL="https://github.com/amutu/zhparser/archive/refs/tags/V2.2.tar.gz"
ARG ZHPARSER_SHA256="1d0b0e834a3d0406a1c520e5b80886cd1c0bf3649d59f13b5f60e8c872a54a31"

# --https-only：URL 写错成 http 时直接失败，而不是静默走明文。
RUN wget --https-only --tries=5 --timeout=60 -O scws.tar.gz "$SCWS_URL" \
    && echo "$SCWS_SHA256  scws.tar.gz" | sha256sum -c - \
    && tar xzf scws.tar.gz \
    && cd scws-1.2.3 \
    && autoreconf -fi \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install \
    && ldconfig \
    && cd /tmp && rm -rf scws-1.2.3 scws.tar.gz

RUN wget --https-only --tries=5 --timeout=60 -O zhparser.tar.gz "$ZHPARSER_URL" \
    && echo "$ZHPARSER_SHA256  zhparser.tar.gz" | sha256sum -c - \
    && tar xzf zhparser.tar.gz \
    && cd zhparser-2.2 \
    && SCWS_HOME=/usr/local make \
    && make install \
    && cd /tmp && rm -rf zhparser-2.2 zhparser.tar.gz

# 清理 build 工具缩小镜像（保留运行时所需 libpcre3 + libscws）
RUN apt-get purge -y --auto-remove \
        build-essential postgresql-server-dev-18 \
        autoconf automake libtool wget \
    && rm -rf /var/lib/apt/lists/* /tmp/*

# 首次 init 时自动跑（postgres_data volume 已存在时不会执行，需手动 apply）
COPY init-zhparser.sql /docker-entrypoint-initdb.d/

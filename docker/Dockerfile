FROM pgvector/pgvector:pg18

# 安装编译工具 + PostgreSQL 18 开发头文件
# 切阿里云镜像（本地网络代理对 deb.debian.org 频繁 502）+ 加 apt 重试
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf 'deb [trusted=yes] http://mirrors.aliyun.com/debian bookworm main\ndeb [trusted=yes] http://mirrors.aliyun.com/debian bookworm-updates main\n' > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/pgdg.list \
    && printf 'deb [trusted=yes] http://mirrors.aliyun.com/postgresql/repos/apt bookworm-pgdg main\n' > /etc/apt/sources.list.d/pgdg.list \
    && printf 'Acquire::Retries "5";\nAcquire::http::Timeout "60";\n' > /etc/apt/apt.conf.d/99retries \
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

# 编译安装 SCWS + zhparser。固定 release tarball 与 sha256，保证新环境可直接构建，
# 不依赖仓库里额外提交二进制源码包。
WORKDIR /tmp
ARG SCWS_URL="https://github.com/hightman/scws/archive/refs/tags/1.2.3.tar.gz"
ARG SCWS_SHA256="98e5932221029f507eadc762c4036eaf95f31f62f45f972401ec6952258feb10"
ARG ZHPARSER_URL="https://github.com/amutu/zhparser/archive/refs/tags/V2.2.tar.gz"
ARG ZHPARSER_SHA256="1d0b0e834a3d0406a1c520e5b80886cd1c0bf3649d59f13b5f60e8c872a54a31"

RUN wget --tries=5 --timeout=60 -O scws.tar.gz "$SCWS_URL" \
    && echo "$SCWS_SHA256  scws.tar.gz" | sha256sum -c - \
    && tar xzf scws.tar.gz \
    && cd scws-1.2.3 \
    && autoreconf -fi \
    && ./configure --prefix=/usr/local \
    && make -j$(nproc) \
    && make install \
    && ldconfig \
    && cd /tmp && rm -rf scws-1.2.3 scws.tar.gz

RUN wget --tries=5 --timeout=60 -O zhparser.tar.gz "$ZHPARSER_URL" \
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

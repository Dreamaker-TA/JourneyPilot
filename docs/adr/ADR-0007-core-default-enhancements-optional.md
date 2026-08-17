# ADR-0007：核心能力默认可用，增强能力显式安装

状态：已采纳
影响：`pyproject.toml` 的 `[dependency-groups]`、`capabilities.py`、`Dockerfile`、
`docker-compose.yml`、`docker/Dockerfile`

## 决定

- **core**：聊天、规划、PostgreSQL/pgvector、Redis、MCP、PDF 导出、基础文档导入。
- **增强**（显式安装）：本地 Qwen Embedding（`local-embedding`）、
  Cross Encoder 重排（`cross-encoder`）、zhparser 中文分词（compose profile）。
- 配置选了某个增强但依赖没装 → **拒绝启动**，并给出那一条安装命令。

## 为什么

让每个用户第一次启动都编译 SCWS + zhparser、都下载 torch，换来的是一个在 ARM 上、
在网络不好的地方、在 apt 源变动那天就装不上的产品。而这两项增强各自只服务一种配置。

拒绝启动而不是降级运行，是因为这一类不匹配**会在第一次真正调用时抛 ImportError** ——
那时候用户已经等了几十秒，而错误信息里没有一条能照着敲的命令。

## 替代方案与为什么没选

- **全部装上**：镜像从 998 MB 涨到几个 GB，且构建面大得多。
- **首次调用时懒加载并降级**：一次检索静默退化成低质量结果，没有一处读数解释为什么。
- **十几个镜像变体**：过早的组合爆炸。只承诺少量官方组合并在 CI 建矩阵。

## 后果

- readiness 有 `optional_capabilities` 一项，**拦门禁**（与「空语料只报不拦」不同）。
- 默认检索退到 `simple` 词法，中文分词质量下降 —— 这一点必须写进 release notes，
  不能用「能运行」冒充「质量相同」。

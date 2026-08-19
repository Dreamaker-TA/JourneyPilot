# 架构决策记录（ADR）

一条 ADR 记的是**一个已经做出的选择、当时的替代方案、以及为什么没选它们**。它不是
设计文档，也不是操作手册。

写 ADR 的判据只有一条：**这个决定被推翻时，会有一批代码同时变错。** 满足这一条的决定
值得一份记录；不满足的（某个阈值取多少、某个函数放哪个文件）留在代码注释里，那里离
它约束的东西更近。

对应关系：ADR 说「为什么这样」，[`../invariants.md`](../invariants.md) 说「所以什么
必须永远成立、谁来保证、哪个测试钉住它」。代码注释因此可以引用一个 invariant 编号，
而不是在第三个文件里重复几百字历史。

| 编号 | 决定 |
|---|---|
| [ADR-0001](ADR-0001-local-single-profile.md) | 固定单一本地身份，不建账户系统 |
| [ADR-0002](ADR-0002-versioned-migrations.md) | 版本化迁移，API 进程不改 Schema |
| [ADR-0003](ADR-0003-sse-terminal-model.md) | 每个请求恰好一个终态帧 |
| [ADR-0004](ADR-0004-durable-local-jobs.md) | 用 PostgreSQL 做单机 durable 命令与任务存储 |
| [ADR-0005](ADR-0005-restart-does-not-resume-spending.md) | 重启后不自动继续花费额度 |
| [ADR-0006](ADR-0006-resource-budgets-and-isolation.md) | 每条高成本通道都有显式预算与隔离 |
| [ADR-0007](ADR-0007-core-default-enhancements-optional.md) | 核心能力默认可用，增强能力显式安装 |
| [ADR-0008](ADR-0008-config-single-source.md) | 配置只有一处定义，来源可查 |
| [ADR-0009](ADR-0009-ci-never-spends-on-providers.md) | CI 不调用真实付费 Provider |
| [ADR-0010](ADR-0010-request-contract-control-plane.md) | 用唯一请求合同与确定性编排控制研究工作流 |
| [ADR-0011](ADR-0011-intent-aware-candidate-pipeline.md) | 意图驱动研究与候选选择分层 |
| [ADR-0012](ADR-0012-intent-fidelity-and-controlled-exploration.md) | 意图保真验收、变更台账与受控探索 |

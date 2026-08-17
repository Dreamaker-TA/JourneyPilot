# ADR-0009：CI 不调用真实付费 Provider

状态：已采纳
影响：`.github/workflows/*`、测试分层

## 决定

PR 与 nightly 的每一个作业都**不花钱**：状态机、数据合同、工具治理、SSE、迁移、
恢复、预算、前端协议全部用 Fake LLM / Fake MCP / 固定 fixture / PostgreSQL 与 Redis
service container 验证。真实 Provider 的 live smoke 是**可选的 release 作业**，
用专用低额度 Key，手动或受保护地触发。

## 为什么

一个每次 PR 都调用付费模型的门禁有两个问题：它花钱，而且**它不稳定** —— 上游一次
限流会让一次无关的改动变红，然后所有人开始忽略红灯。合同测试要验证的是我们的代码，
不是上游今天的心情。

## 替代方案与为什么没选

- **每次 PR 跑一小段真实调用**：不确定性进入门禁，红灯失去意义。
- **完全不测 Provider 交互**：那些兼容逻辑（json_schema 降级、reasoning 方言、
  tool 消息顺序）恰恰是事故最多的地方。它们改由 capability 声明 + 单元测试承载
  （见 [ADR-0008](ADR-0008-config-single-source.md)）。

## 后果

- Fake LLM 必须支持 invoke / stream / tool calls / 畸形 JSON / 超时 / 可重试错误 /
  usage 元数据 / 延迟与取消。只返回固定字符串的 fake 验证不了工作流边界。
- 重型矩阵（10k 事件、kill-point 全矩阵、多架构镜像、备份恢复全量 fixture）进
  nightly 与 release，不进每个 PR。

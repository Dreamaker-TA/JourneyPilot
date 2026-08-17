# ADR-0006：每条高成本通道都有显式预算与隔离

状态：已采纳
影响：`api/sse_buffer.py`、`services/blocking_work.py`、`utils/concurrency.py`、
`rag/sources/document_parse.py`、`entities/run_budget.py`、`workflows/run_budget.py`

## 决定

四类边界，每一类都有配置项和可观察的读数：

| 边界 | 谁持有 |
|---|---|
| 容量 | `SSEBuffer`（分类缓冲）、`IngestConfig`（字节/页/条目/展开量） |
| 并发 | `BlockingWorkConfig`（线程通道）、`ProviderChannelConfig`（上游通道） |
| 时间 | `RunDeadlineConfig`（四段窗口）、`IngestConfig.parse_timeout_seconds` |
| 费用/调用 | `RunBudgetConfig` → `RunBudgetSnapshot`（Run 授权时封存） |

事件分三类：critical 不可丢（队列满则生产者等待）、coalescible 可合并（token 合并成
更大 chunk，**不丢字符**）、ephemeral 可覆盖。

## 为什么

单用户不能自动消除性能问题：一次 Deep Run、一个很长会话、一份复杂文档，任意一个都
足以让内存无界增长或让 Event Loop 停住。而没有上限的通道在出问题时**没有任何读数能
解释发生了什么** —— 用户只感觉「变慢了」。

费用这一维必须在**调用之前**判：只在调用后记账拦不住超支。

## 替代方案与为什么没选

- **一个 `asyncio.Queue(maxsize=200)` 当全部答案**：terminal 不可丢与 token 可合并
  是两种语义，同一个队列给不了两种保证。
- **靠超时保护解析**：线程里的 Python/C 代码杀不掉，`wait_for` 超时只停止*等待*。
  所以不可信解析走子进程 + RLIMIT，并且**先有输入上限**。
- **让连接池容量当预算**：底层池上限 1000 不代表允许一次上传发 1000 条模型调用。

## 后果

- 预算耗尽是一个**有名字的原因**（`run_budget_exhausted.<维度>`），不是「模型失败」。
- 价格表未命中时费用低报，所以费用这一维不参与判定（`cost_complete=false`），
  低报本身被报出去。
- 背压不能持有数据库事务或 Provider 连接：事件在提交之后才入 buffer。

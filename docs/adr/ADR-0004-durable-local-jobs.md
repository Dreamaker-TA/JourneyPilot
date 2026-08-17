# ADR-0004：用 PostgreSQL 做单机 durable 命令与任务存储

状态：已采纳
影响：`trip_run_commands`、`background_jobs`、`run_execution`、
`services/run_commands.py`、`services/background_jobs.py`、`workflows/run_control.py`

## 决定

不引入外部消息队列。取消、追加要求、后台任务（记忆抽取等）都是**先落库的行**，
由当前进程的 asyncio worker 领取；进程内的 registry 只是一个降低轮询延迟的通知通道。

    PostgreSQL durable row  ← 最终事实
    + 当前进程 asyncio worker
    + in-process Event      ← 只是缓存，丢了最多多等一个轮询间隔

## 为什么

产品边界是单机单进程，不需要 Celery/Kafka 的协调能力。但「单机」不等于「所有状态
只放在内存里」：`ensure_future` 出去的记忆抽取在关闭程序那一刻就没了，而
「取消」如果只存在于发请求命中的那个内存 registry 里，那么换一个进程就点不动。

判据是：**关掉程序之后，这件事还应该发生吗？** 答案是「是」的东西必须有一行。

## 替代方案与为什么没选

- **外部 broker**：给一个自托管单人应用增加一个必须运维的组件。
- **纯内存 + 尽力而为**：状态会说谎（UI 显示 running 的 Run 已经没有执行器了）。

## 后果

- 所有 job/command 共享 lease、attempt、backoff、dedupe 与诊断合同。
- 命令至少消费一次，所以去重判据必须落在 state 里而不是内存里。

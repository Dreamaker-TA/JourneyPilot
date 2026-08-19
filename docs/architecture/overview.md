# 架构总览

JourneyPilot 是一个**单机、单进程、单用户**的自托管旅行规划应用。这一句是全部架构决定
的前提：没有账户系统（[ADR-0001](../adr/ADR-0001-local-single-profile.md)）、没有外部
消息队列（[ADR-0004](../adr/ADR-0004-durable-local-jobs.md)）、没有多 API worker。

```text
浏览器 (React/Vite)
  │  SSE + REST
  ▼
FastAPI 进程
  ├─ LangGraph 工作流（研究 → 门 → 编排 → 交付）
  ├─ 进程内 asyncio worker（durable command / background job 的消费者）
  ├─ 受限线程与子进程（PDF 渲染、文档解析、本地推理）
  └─ MCP stdio 子进程（外部数据源）
       │
       ▼
PostgreSQL + pgvector（最终事实）        Redis（缓存与快照）
```

## 三条边界

**一、最终事实在库里，内存只是缓存。** 关掉程序之后还应该发生的事，都有一行。
`trip_run_commands`、`background_jobs`、`run_execution` 是三个例子；进程内的 registry
只负责把延迟从「一个轮询周期」降到「立刻」。

**二、Schema 只由迁移改。** API 进程不执行 DDL，只做一次只读的结构合同校验
（[ADR-0002](../adr/ADR-0002-versioned-migrations.md)）。

**三、每条高成本通道都有显式上限，而且上限可以被看见。**
容量、并发、时间、费用四类边界各有配置项与读数
（[ADR-0006](../adr/ADR-0006-resource-budgets-and-isolation.md)），
运营者从 `GET /api/health/ready` 与 `journeypilot doctor` 读它们。

## 一次请求走过什么

1. **路由**：简单问题走快问快答；多日行程进研究工作流。
2. **合同化**：一次归一化同时产生 clause ledger、`IntentSpec` 与 Constraint Pack，
   并封装为唯一 `RequestContract`。
3. **编排**：从合同确定性投影 `ResearchBriefV2`、`ResearchQueryPlan`、
   `CapabilityPlan` 与 `MinimumDeliveryDraft`；每个 hard intent 都有所有者与验收标准。
4. **授权**：用户在计划门确认后封存 Draft，同时封存 `RunDeadlineSnapshot` 与
   `RunBudgetSnapshot` —— 时间与钱的上限从这一刻起对这个 Run 固定。
5. **研究**：并行 Worker 按 assignment 中的 Query ID 执行 Intent Primary 与 Structural
   查询；只有前序查询不足且政策允许时才执行 Generic Fallback。每个 Research Packet
   绑定当前 `PlanningGeneration`、Query Plan 和服务端生成的 Candidate Discovery Lineage。
6. **候选门**：Admission 验证事实与硬 Constraint；Candidate Intent Evaluation、Ranking
   和 Selection 分别判断意图匹配、分层顺序与本次候选集合。缺少高优先级意图候选时，
   Candidate Gate 生成 Targeted Repair Query。
7. **编排**：Itinerary Planner 只消费 `CandidateSelectionPlan` 允许的候选，按
   `CompositionRule` 填充合法槽位，并记录每次确定性后处理的 `CompositionMutation`。
8. **验收**：Intent Fidelity Gate 检查数量、频率、时段、顺序、硬排除和解释要求；缺口回到
   对应所有者，预算耗尽的义务变成公开偏差，禁止型硬规则不能降级。
9. **交付**：一次原子提交写出唯一的 v10 `DeliveryBundle`；Workspace 保存意图快照、选择计划、
   变更台账和覆盖报告，所有面向用户的界面都是公共安全投影。

默认选择是确定性的。只有明确的 Diversity/Alternatives Intent 才进入受控 Explore；seed 只在
已通过事实准入和硬合同的近分候选之间生效，因此同 seed 可回放，硬要求不会随探索变化。

计划门修改和运行中追加要求不直接改 prompt。它们先变成 `IntentAmendment`，
回到同一个 Request Contract 边界，再按影响范围失效旧产物。

## 从哪里继续读

- 决定与它们的替代方案：[`../adr/`](../adr/)
- 什么必须永远成立、谁保证、哪个测试钉住它：[`../invariants.md`](../invariants.md)
- 配置字段、默认值、环境变量：[`../configuration.md`](../configuration.md)（生成物）

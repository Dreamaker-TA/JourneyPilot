# ADR-0010：用唯一请求合同与确定性编排控制研究工作流

## 决定

每次深度规划先把受控旅行身份之外的用户输入分解为 clause，用一次结构化
归一化同时产生 `IntentSpec` 与 Constraint draft，再封装为 `RequestContract`。

`ResearchBriefV2`、`CapabilityPlan` 与执行图由服务端纯函数确定性生成。LLM 不决定
节点、输入边、意图所有权或代际隔离。

计划门修改与运行中 supplement 统一转成 `IntentAmendment`，通过同一归一化边界
产生新 `PlanningGeneration`。Research Packet、Catalog、Workspace 与 Delivery Manifest 必须
绑定同一 generation。

## 理由

同一句要求如果分别被 Brief、Constraint 抽取、Planner 与 Worker prompt 解释，其含义、
优先级与验收标准会随调用点漂移。先建立唯一合同，再做确定性投影，可以让每个
hard intent 都能被追踪到所有者、成功标准与最终产物。generation 则把运行中修改与
并行晚到结果隔开，防止旧研究进入新计划。

## 替代方案与未采用理由

- 继续让 LLM Planner 直接生成图：无法稳定证明 hard intent 的所有权与覆盖。
- 在 Worker prompt 末尾追加补充要求：绕过 clause ledger、冲突检查与失效策略。
- 为旧 Brief 和旧 Packet 保留兼容读路径：会继续产生两套事实源，因此直接替换合同。

## 结果

新的合同版本是破坏性变更。不保留旧 checkpoint、旧 Research Brief 字符串或旧 Packet
的读取路径；本地测试数据与 fixture 一次性升级到新合同。

## 对应不变量

INV-INTENT-001、INV-INTENT-002、INV-INTENT-003、INV-INTENT-004、
INV-INTENT-005、INV-PLAN-001。

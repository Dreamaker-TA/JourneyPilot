# ADR-0012：意图保真验收与受控探索

## 决定

行程组合只读取 `CandidateSelectionPlan` 中允许进入 Composition 的候选。确定性
`CompositionRule` 负责数量、频率、时段、顺序、必须包含和禁止项；任何自动删改、移动、
回填或调时都写入 `CompositionMutation`，并在继续交付前重跑硬规则。

Typed Artifact Gate 之后增加独立的 Intent Fidelity Gate。它生成
`IntentCoverageReport`，并标记缺口应由 Candidate Gate、Itinerary Planner 或输出投影负责；
工作流将候选不足重新路由到 Candidate Gate，其余组合与投影缺口进入有预算的组合修复。
禁止型硬规则永不降级；义务型硬规则在修复预算耗尽后转为公开偏差；软偏好不阻断，但不能
被声明为完全满足。

默认 Selection Policy 保持确定性且没有 seed。只有正式 `DIVERSITY` 或 `ALTERNATIVES`
Intent 才启用 Explore；seed 只重排已经通过事实准入和硬要求、且排名接近的候选，不改变
Composer 的零温度设置。相同 seed 必须产生相同 Selection Plan。

Trip Workspace 与 Delivery Bundle 升级为 v10。正式 Workspace 同时保存 Intent Snapshot、
Selection Plan、Mutation Ledger 与 Coverage Report；公开投影只给出要求摘要、满足状态和说明，
不公开原始文本位置、内部优先级、候选 ID、分数、Prompt 或修复细节。本项目没有历史用户数据，
因此旧 v9 合同直接失效，不提供兼容读取层。

## 理由

Schema 只能保证单个对象的形状，不能证明“每天最多两个”“每天下午一次”或“禁止博物馆”
这类跨实体规则。把这些判断留给 Composer 或 Delivery Quality Gate，会让调研、选择、排程和
文案各自解释一遍用户要求，也无法区分事实错误、候选不足与组合错误。独立保真门让每条要求
都有确定状态、修复责任和耗尽语义。

随机提高模型温度只能产生不可回放的文字差异，不能证明方案真的多样。把探索限制在合规候选
的 Selection 层，才能同时保留事实真实性、硬合同和可复现性。

## 替代方案与未采用理由

- 让 Delivery Quality Gate 同时验收 Intent：会混淆结构完整性与用户意图保真，修复路由不清。
- 把所有硬要求都做成永久阻断：无法满足的义务会耗尽后死循环，用户也得不到明确偏差。
- 让软偏好在报告文案中“补齐”：没有 Candidate 与 Entity 绑定，属于不可验证的声称。
- 提高 Composer temperature 产生多方案：差异不可归因、不可用 seed 回放，也可能改变硬约束。
- 保留 v9 读取分支并补空 Coverage：旧交付物没有经过新门，补空报告会伪造验收事实。

## 结果

运行完成审计记录 Intent、Query、Catalog、Selection、Workspace 与 Coverage 的版本和 Hash，
`run_diff` 能定位两次运行的差异在哪一层出现或消失。用户编辑或天气刷新 Workspace 后必须
重新生成 Coverage；破坏禁止型硬规则的编辑在提交前失败。

## 对应不变量

INV-COMPOSITION-001、INV-FIDELITY-001、INV-MUTATION-001、
INV-EXPLORE-001、INV-REPLAY-001。

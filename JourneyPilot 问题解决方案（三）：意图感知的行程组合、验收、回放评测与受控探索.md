# JourneyPilot 问题解决方案（三）：意图感知的行程组合、验收、回放评测与受控探索

## 0. 文档信息

**建议仓库路径**

```text
docs/implementation/agent-orchestration/
03-intent-aware-composition-fidelity-evaluation.md
```

**前置条件**

文档一和文档二已经全部完成，系统已有：

- IntentSpec；
- PlanningGeneration；
- ResearchBriefV2；
- CapabilityPlan；
- ResearchQueryPlan；
- Candidate Discovery Lineage；
- Truth Admission；
- Candidate Intent Evaluation；
- Candidate Ranking；
- CandidateSelectionPlan。

**本批次覆盖**

- P0-C：Intent-aware Composition 与 Intent Fidelity Gate。
- P1-B：可观测性、回放和 Run Diff。
- 相关 P2：
  - Controlled Explore Mode；
  - Prompt/Model/Policy 版本归因；
  - Reducer 和旧状态清理；
  - 合同版本迁移；
  - 端到端行为评测；
  - 修复后次生问题收口。

---

# 1. 当前 Composer 的具体问题

当前 Itinerary Planner 会按：

```text
budget_fit
weather_fit
constraint_fit
```

中的最弱项排序候选，并以 Candidate ID 稳定打破平局。它暴露给 Composer 的能力信息主要包括名称、类型、目的地和这三个 Fit。

这意味着上游即使知道：

```text
候选 A 特别适合当代建筑摄影
候选 B 只是普通热门景点
```

如果这种语义没有进入 `_placement_capabilities`，Composer 仍然无法稳定利用。

更重要的是，当前确定性补位会：

- 补入未安排的长途交通；
- 将未放置的 admitted Visit/Dining 放到空白日；
- 补足 required kind；
- 在模型完全没有放置内容时重建基本 Skeleton。

这些机制保证了结构收束，但可能破坏用户要求。

最终 Delivery Quality Gate 主要验证：

- 日期；
- Timeline；
- 住宿夜；
- 餐饮日；
- 候选重复；
- Connector；
- 跨城交通；
- Source；
- Weather；
- Selection Slot。

它明确是结构和投影质量门，不验证用户 Intent。

---

# 2. 目标链路

```text
CandidateSelectionPlan
        │
        ▼
Composition Rule Compiler
        │
        ▼
Intent-aware Itinerary Composer
        │
        ▼
Deterministic Validation
        │
        ▼
Mutation-aware Repair / Backfill
        │
        ▼
Intent Fidelity Gate
        │
        ├── Targeted Research
        ├── Reselection
        ├── Recomposition
        ├── Reprojection
        └── Explicit Deviation
                │
                ▼
Existing Delivery Quality Gate
                │
                ▼
Deterministic Projection
```

关键原则：

```text
Intent Fidelity Gate
    = 这是用户要的吗？

Delivery Quality Gate
    = 这份结果结构上能否安全投影？
```

两个 Gate 不得合并。

---

# 3. Composition Rule Compiler

建议新增：

```text
src/travel_agent/entities/composition_rules.py
src/travel_agent/services/composition_rule_compiler.py
```

## 3.1 Rule 类型

```python
class CompositionRuleKind(str, Enum):
    MUST_PLACE = "must_place"
    MUST_NOT_PLACE = "must_not_place"
    MAX_PER_DAY = "max_per_day"
    MIN_PER_DAY = "min_per_day"
    CADENCE = "cadence"
    TIME_WINDOW = "time_window"
    SEQUENCE = "sequence"
    DESTINATION_SCOPE = "destination_scope"
    REST_WINDOW = "rest_window"
    MAX_TRAVEL_TIME = "max_travel_time"
    OUTPUT_EXPLANATION = "output_explanation"
```

```python
class CompositionRule(StrictModel):
    rule_id: str
    intent_id: str
    generation_id: str

    rule_kind: CompositionRuleKind
    target_domain: Optional[ResearchDomain]

    hard: bool
    policy_on_failure: Literal[
        "never_violate",
        "repair_then_deviate",
        "nonblocking_preference",
    ]

    parameters: CompositionRuleParameters
```

## 3.2 失败政策必须区分

### Prohibitive Hard Rule

例如：

```text
不要博物馆
每天最多两个景点
禁止飞机
```

政策：

```text
never_violate
```

即使无法填满行程，也不能违反。

### Obligation Hard Rule

例如：

```text
必须包含浅草寺
每天下午安排咖啡馆
```

政策：

```text
repair_then_deviate
```

优先修复，无法满足时明确披露，不能假装满足。

### Soft Preference

例如：

```text
整体尽量安静
偏小众
```

政策：

```text
nonblocking_preference
```

参与排序和主题，但不制造无限修复循环。

---

# 4. Composer 只能读取 CandidateSelectionPlan

当前 `_composition_response_schema` 会将 admitted Candidate ID 写入合法枚举。

应改为：

```text
admitted candidate ids
→ selected candidate ids
```

Composer 输入：

```python
class CompositionInput(StrictModel):
    generation_id: str

    intent_contract: IntentContractSnapshot
    composition_rules: List[CompositionRule]

    selected_candidates: List[SelectedCandidateCapability]
    alternative_candidates: List[SelectedCandidateCapability]

    minimum_delivery_draft: MinimumDeliveryDraft
    controlled_trip_identity: ControlledTripIdentity
    constraint_pack: ConstraintPack

    previous_mutations: List[CompositionMutation]
    repair_context: Optional[CompositionRepairContext]
```

## 4.1 Selected Candidate Capability

```python
class SelectedCandidateCapability(StrictModel):
    candidate_id: str
    candidate_kind: str
    destination_id: str

    selection_role: str
    rank: int

    matched_intent_ids: List[str]
    unknown_intent_ids: List[str]
    hard_violation_intent_ids: List[str]

    selection_reasons: List[str]
    evidence_confidence: float

    budget_fit: float
    weather_fit: float
    constraint_fit: float

    place_id: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]

    schedule_capabilities: Dict[str, Any]
```

Composer 不再只看到：

```json
{
  "candidate_id": "...",
  "name": "...",
  "budget_fit": 1,
  "weather_fit": 1,
  "constraint_fit": 1
}
```

---

# 5. Composer Prompt 与 Schema

## 5.1 Prompt 输入

必须包括：

- Intent Contract Snapshot；
- Composition Rules；
- Selected Candidates；
- 每个候选覆盖的 Intent；
- 不得违反的 Rule；
- Minimum Delivery Day Shell；
- 交通和住宿结构责任；
- Previous Mutation 和 Repair Context。

不再将原始用户长文本作为唯一语义依据。

## 5.2 继续使用严格 JSON Schema 和低随机性

Composer 的结构输出继续使用：

```text
temperature = 0
strict JSON Schema
```

不要通过提高温度解决个性化问题。

## 5.3 Schema 无法表达的跨字段规则

以下规则很难完全由 JSON Schema 表达：

- 每天最多两个 Visit；
- 每天下午一个 Cafe；
- 某地点必须在另一个地点之前；
- 每个 Destination 至少覆盖一个主题候选；
- 同一区域聚类；
- 长途交通前后留缓冲。

这些规则由：

```text
Composition Rule Validator
```

在模型输出后确定性验证。

---

# 6. 重构 Backfill

## 6.1 当前错误语义

当前 Backfill 有类似：

```text
admitted Visit/Dining 如果未被放置
    → 尝试填入空白日
```

新语义必须是：

```text
被 SelectionPlan 选中
+
满足当前 Rule
+
存在合法 Slot
    → 才能补位
```

## 6.2 Slot-aware Backfill

建议实现：

```python
def build_legal_open_slots(
    draft: ItineraryCompositionDraft,
    rules: Sequence[CompositionRule],
) -> List[OpenCompositionSlot]:
    ...
```

每个 Slot 包括：

```text
day
time window
destination
允许的 candidate kind
剩余数量
已有相邻地点
交通可行性
待覆盖 intent
```

补位时按：

```text
高优先级尚未覆盖 Intent
→ Rule 合法性
→ 区域和交通可行性
→ Candidate Rank
→ 稳定 ID
```

选择。

## 6.3 不再追求“每个 admitted candidate 都出现”

允许：

- 行程存在空闲时间；
- 某日只有一个主要景点；
- Generic Fallback 不被使用；
- 未被选择的 admitted Candidate 永远不进入 Workspace。

## 6.4 长途交通仍是结构锚点

长途交通、住宿夜和跨城责任仍然优先保证。

但任何锚点补入后，都必须重新验证受影响 Intent。

---

# 7. Authored Place 的限制

当前 Composer 可以在 Catalog 供给不足时创作 Visit、Dining 或 Route，然后解析真实地点身份。

这个能力不能直接删除，但必须被严格限制。

## 7.1 Authored Place 不能绕过 Intent Pipeline

Authored Place 在解析真实身份后，必须经过：

```text
Identity Resolution
    ↓
Truth Verification
    ↓
Candidate Intent Evaluation
    ↓
Composition Rule Validation
```

不能因为它由 Composer 写出，就免于：

- 排除规则；
- 地域范围；
- 属性验证；
- Intent Coverage；
- Source Honesty。

## 7.2 不能编造语义属性

若 Composer 写出一家咖啡馆，但没有外部证据证明它“安静、适合摄影”：

```text
候选可以作为普通 Cafe
但不能用于证明“安静、适合摄影”已满足
```

## 7.3 Authored Fallback 的使用条件

只有：

```text
Selected Candidate Pool 无法满足基本结构
+
不存在 prohibitive rule 冲突
+
仍有 Composition Budget
```

才允许。

其 origin 必须是：

```text
composer_authored_fallback
```

---

# 8. Composition Mutation Ledger

当前裁剪、移动、替换和补位会改变模型草稿，但系统缺少统一的“这次修改牺牲了什么”的记录。

建议新增：

```text
src/travel_agent/entities/composition_mutation.py
```

```python
class CompositionMutationType(str, Enum):
    DROP = "drop"
    MOVE = "move"
    REPLACE = "replace"
    BACKFILL = "backfill"
    REORDER = "reorder"
    TIME_ADJUST = "time_adjust"


class CompositionMutation(StrictModel):
    mutation_id: str
    generation_id: str

    mutation_type: CompositionMutationType
    reason_code: str

    source_entity_ids: List[str]
    target_entity_ids: List[str]

    affected_intent_ids: List[str]
    affected_rule_ids: List[str]

    coverage_before: Dict[str, str]
    coverage_after: Dict[str, str]

    hard_rules_revalidated: bool
    created_by: Literal[
        "deterministic_pruner",
        "anchor_backfill",
        "slot_backfill",
        "composition_repair",
        "user_edit",
    ]
```

## 8.1 所有后处理函数改成显式返回 Mutation

不要：

```python
mutate_payload_in_place(payload)
```

应使用：

```python
updated_payload, mutations = apply_backfill(...)
```

## 8.2 Mutation 后强制重新验证

任何 Mutation 如果导致：

```text
hard intent 从 satisfied 变成 unsatisfied
```

必须：

- 回滚 Mutation；
- 尝试其他 Candidate；
- 路由 Recomposition；
- 或形成明确 Deviation。

不能直接进入 Projection。

---

# 9. Intent Fidelity Gate

建议新增：

```text
src/travel_agent/entities/intent_coverage.py
src/travel_agent/agents/orchestrator/intent_fidelity_gate.py
src/travel_agent/services/intent_verification.py
```

## 9.1 Coverage 状态

```python
class IntentCoverageStatus(str, Enum):
    SATISFIED = "satisfied"
    PARTIALLY_SATISFIED = "partially_satisfied"
    UNSATISFIED = "unsatisfied"
    UNVERIFIABLE = "unverifiable"
    UNSUPPORTED = "unsupported"
    CONFLICTED = "conflicted"
```

```python
class IntentCoverageItem(StrictModel):
    intent_id: str
    status: IntentCoverageStatus

    supporting_entity_refs: List[EntityRef]
    supporting_candidate_ids: List[str]
    supporting_fact_assertion_ids: List[str]

    violated_entity_refs: List[EntityRef]

    covered_days: List[int]
    missing_days: List[int]

    verification_mode: VerificationMode
    public_explanation: str

    blocking: bool
```

```python
class IntentCoverageReport(StrictModel):
    schema_version: Literal["journeypilot.intent_coverage.v1"]

    coverage_report_id: str
    generation_id: str
    intent_spec_revision: int
    workspace_revision: int

    items: List[IntentCoverageItem]

    hard_satisfaction_rate: float
    soft_coverage_rate: float

    blocking_gap_ids: List[str]
    deviations: List[IntentDeviation]

    content_hash: str
```

## 9.2 确定性检查

无需 LLM：

- 必须地点是否存在；
- 禁止类别是否出现；
- 每天 Visit 数量；
- 每天 Cafe 数量；
- 时间窗口；
- 顺序；
- 每晚住宿；
- 交通方式；
- 备选数量；
- 输出解释是否存在；
- 每个目标实体是否有对应说明；
- 是否重复；
- 是否有 Selection Slot。

## 9.3 语义检查

优先复用文档二的 Candidate Intent Evaluation。

只有在“整份组合是否整体体现某个软主题”无法由 Candidate Match 聚合得到时，才进行一次批量语义评估。

不要再次让一个 Judge 阅读整份大报告并自由打分。

## 9.4 Intent Fidelity Gap

```python
class IntentFidelityGap(StrictModel):
    gap_id: str
    intent_id: str

    reason: Literal[
        "required_candidate_missing",
        "excluded_candidate_present",
        "quantity_rule_violated",
        "cadence_rule_missing",
        "time_window_violated",
        "sequence_rule_violated",
        "semantic_coverage_low",
        "output_requirement_missing",
        "intent_evidence_unavailable",
    ]

    blocking: bool

    retry_target: Literal[
        "candidate_gate",
        "candidate_selection",
        "itinerary_planner",
        "delivery_projection",
        "none",
    ]

    affected_entity_ids: List[str]
    repair_context: Dict[str, Any]
```

---

# 10. Gate 责任与循环政策

## 10.1 Gate 顺序

```text
Candidate Gate
    = 是否有真实可用的候选

Intent Fidelity Gate
    = 选择和行程是否落实用户要求

Delivery Quality Gate
    = 最终结构是否可投影
```

## 10.2 修复路由

| Gap | Owner | Retry |
|---|---|---|
| 没有符合 Intent 的真实候选 | Candidate Gate | Targeted Research |
| 有候选但 Selection 未选 | Candidate Selection | Reselect |
| 已选但没放入行程 | Itinerary Planner | Recompose |
| 数量、时段、顺序违反 | Itinerary Planner | Recompose |
| 输出解释缺失 | Projection | Reproject |
| 软主题覆盖不足 | Fidelity Gate | 通常披露，不循环 |
| Provider 无法验证属性 | Fidelity Gate | Unverifiable Deviation |

## 10.3 Budget 与 Deadline

不要为每个 Intent 单独创建无限重试预算。

复用现有：

- Candidate Research Budget；
- Composition Repair Budget；
- Research Window；
- Composition Window；
- Delivery Deadline。

Intent Gap 只决定把现有预算花在哪里。

## 10.4 耗尽后的行为

### 禁止型硬规则

例如不要博物馆：

```text
永不允许违反
宁可少放一个景点
```

### 义务型硬规则

例如必须安排某地点：

```text
修复预算耗尽
→ 不再无限循环
→ 标记 unsatisfied
→ 明确说明原因
→ 仍可交付诚实的降级 Bundle
```

### 软偏好

```text
不阻断
但不得声称已完全满足
```

---

# 11. Workspace 与 Delivery Bundle

当前 `TripWorkspaceV2` 已经包含：

- `user_input_anchors`；
- `selection_slots`；
- `personalization_influences`。

建议升级：

```text
journeypilot.trip_workspace.v7
→ journeypilot.trip_workspace.v8
```

新增：

```python
intent_contract_snapshot: IntentContractSnapshot
intent_coverage_report: Optional[IntentCoverageReport]
composition_mutations: List[CompositionMutation]
candidate_selection_plan: CandidateSelectionPlan
```

正式 Bundle 升级：

```text
journeypilot.delivery_bundle.v7
→ journeypilot.delivery_bundle.v8
```

增加公共安全字段：

```python
class PublicRequirementFulfillment(StrictModel):
    requirement_id: str
    summary: str
    status: Literal[
        "satisfied",
        "partially_satisfied",
        "unsatisfied",
        "unverifiable",
    ]
    explanation: str


class PublicFulfillmentSummary(StrictModel):
    fulfilled: List[PublicRequirementFulfillment]
    deviations: List[PublicRequirementFulfillment]
```

不要公开：

- 原始 source span；
- 内部优先级；
- 模型分数；
- Prompt；
- 内部 Intent Hash；
- Candidate ID；
- Gate repair 细节。

---

# 12. 用户要求的解释不应产生动态 Schema

例如：

```text
“每个地点解释为什么适合摄影。”
```

不要为每种用户要求动态新增：

```text
photography_reason
architecture_reason
family_reason
quiet_reason
```

这会让 Delivery Schema 无法维护。

建议使用通用结构：

```python
class EntityIntentExplanation(StrictModel):
    intent_id: str
    label: str
    explanation: str
    evidence_basis: Literal[
        "verified_fact",
        "supported_description",
        "planning_judgment",
    ]
```

每个公共实体可带：

```python
intent_explanations: List[EntityIntentExplanation]
```

Intent Fidelity Gate 可确定性验证：

```text
目标为所有 Visit
且要求解释摄影价值
→ 每个 Visit 必须存在对应 intent_explanation
```

---

# 13. PersonalizationInfluence 的生成

正式流程：

```text
IntentCoverageItem
    ↓
Entity Binding
    ↓
PersonalizationInfluence
    ↓
EntityLineage.personalization_influence_ids
    ↓
Projection
```

Projection 当前已经能把 Lineage 中的 Influence ID 投影到报告块。

需要补齐的不是传递，而是可靠的生产者。

规则：

- 只为实际影响 Filter、Ranking、Selection 或 Schedule 的 Intent 创建；
- 不为所有 Context 创建噪声 Influence；
- Influence 必须引用正式 `intent_id`；
- `display_text` 来自规范化 public summary，不来自内部 Prompt。

---

# 14. Controlled Explore Mode

## 14.1 默认仍然确定性

默认：

```text
mode = deterministic
selection_seed = None
```

相同输入、相同事实快照、相同版本和相同政策，应得到相同语义结果。

## 14.2 Explore Mode

只有用户明确要求：

```text
再来一套
换一批地点
给三个不同风格版本
更小众一点
```

才进入 Explore。

```python
class SelectionPolicy(StrictModel):
    mode: Literal["deterministic", "explore"]

    selection_seed: Optional[int]
    alternative_count: int
    diversity_strength: float

    avoid_previous_candidate_ids: List[str]
    preferred_theme_clusters: List[str]

    policy_version: str
```

## 14.3 随机性边界

Explore 只能发生在：

```text
已通过 Truth Admission
已满足硬 Intent
排名接近
```

的候选之间。

不能改变：

- 日期；
- 目的地；
- 必须地点；
- 禁止类别；
- 健康约束；
- 预算上限；
- 固定交通；
- Provider 真实性要求。

## 14.4 不改变 Composer Temperature

Composer 继续：

```text
temperature = 0
```

多样性由：

- Query Variant；
- Candidate Cluster；
- MMR；
- Selection Seed；
- Previous Candidate Exclusion；

产生，不由自由生成噪声产生。

## 14.5 可复现

```text
相同 seed
→ 相同 Selection Plan
→ 相同 Composition Input
→ 相同语义 Workspace
```

---

# 15. 运行可观测与回放

当前 TripRun 已经把 `completion_audit` 作为独立的 Developer/Eval 持久化投影，与普通状态摘要分开。

应扩展而不是另起一套日志系统。

## 15.1 Completion Audit 增加

```json
{
  "planning_generation": {
    "generation_id": "...",
    "identity_hash": "...",
    "intent_hash": "...",
    "constraint_hash": "..."
  },
  "versions": {
    "intent_schema": "...",
    "query_policy": "...",
    "ranking_policy": "...",
    "selection_policy": "...",
    "composition_policy": "...",
    "fidelity_policy": "...",
    "prompt_versions": {},
    "model_versions": {}
  },
  "research": {
    "query_plan_hash": "...",
    "executed_query_ids": [],
    "provider_snapshot_hashes": [],
    "generic_fallback_usage_count": 0
  },
  "selection": {
    "catalog_hash": "...",
    "selection_plan_hash": "...",
    "selection_seed": null
  },
  "composition": {
    "workspace_hash": "...",
    "mutation_count": 0,
    "postprocessor_override_count": 0
  },
  "intent_fidelity": {
    "hard_satisfaction_rate": 1.0,
    "soft_coverage_rate": 0.75,
    "unsatisfied_intent_ids": [],
    "unverifiable_intent_ids": []
  }
}
```

不在 Audit 中保存原始用户长文本。

## 15.2 Run Diff

建议新增：

```text
src/travel_agent/services/run_diff.py
```

输出：

```text
Input Diff
Intent Diff
Constraint Diff
Query Diff
Provider Snapshot Diff
Candidate Diff
Admission Diff
Ranking Diff
Selection Diff
Composition Diff
Mutation Diff
Coverage Diff
Projection Diff
```

当两次结果一致时，可以准确回答：

```text
因为 Query 和 Provider Snapshot 相同
还是因为 Ranking 相同
还是 Composer 抹平了 Selection 差异
```

## 15.3 核心指标

至少记录：

```text
intent_clause_coverage_rate
intent_assignment_coverage_rate
hard_intent_satisfaction_rate
soft_intent_coverage_rate
intent_candidate_coverage_rate
generic_fallback_usage_rate
postprocessor_override_rate
unexplained_mutation_count
semantic_delta_sensitivity
selection_overlap_rate
explore_diversity_rate
```

---

# 16. 行为评测体系

## 16.1 不测试完整自然语言相等

不要将整份报告文本作为 Golden Snapshot。

应断言结构语义：

```text
某类别不得出现
某 Intent 必须由实体支持
每日数量不超过限制
Query Plan 必须变化
Selection Plan 必须变化
硬合同必须保持
```

## 16.2 Repeatability Test

相同：

```text
Input
IntentSpec
Provider Fixture
Model Fixture
Policy Version
Seed
```

断言：

```text
Query Plan Hash 相同
Catalog Hash 相同
Selection Plan Hash 相同
Workspace Hash 相同
Coverage 相同
```

## 16.3 Sensitivity Test

只修改一条要求：

```text
无关部分保持稳定
相关 Query / Ranking / Selection / Placement 发生预期变化
```

这是判断当前问题是否真正解决的核心测试。

## 16.4 Explore Test

不同 Seed：

```text
硬 Intent 满足率相同
事实准入全部通过
可选候选存在变化
Selection Overlap 低于阈值
```

---

# 17. 必须覆盖的端到端场景

## 场景 1：传统建筑与当代建筑

断言：

- Query 不同；
- Ranking 不同；
- Selection 不同；
- 最终 Visit 主题覆盖不同；
- Identity、日期和预算不变。

## 场景 2：禁止博物馆

断言：

- Query 不调用 museum fallback；
- Catalog 可包含其他真实 Visit；
- Selection 和 Workspace 无博物馆；
- Backfill 不得补入博物馆。

## 场景 3：每天最多两个主要景点

断言：

- 每日 Visit 数量不超过 2；
- Backfill 不突破；
- 空闲时间允许存在。

## 场景 4：每天下午咖啡馆

断言：

- 每天有 Dining/Cafe；
- 时间位于下午；
- 缺失日明确进入 Coverage Gap。

## 场景 5：摄影解释

断言：

- 每个目标 Visit 有 EntityIntentExplanation；
- Projection 不丢失；
- 缺一项就触发 projection repair。

## 场景 6：住宿主题变化

断言：

- 通用酒店事实可以复用；
- Ranking 和 Selection 不同；
- 选择理由引用对应 Intent。

## 场景 7：必须包含具体地点

断言：

- Candidate Research 优先解析该地点；
- Selection 锁定；
- Composer 必须放置；
- 不可用时明确 deviation。

## 场景 8：禁止飞机

断言：

- Transport Admission/Selection 无 flight；
- Explore Seed 不得改变此结果。

## 场景 9：运行中早期 supplement

断言：

```text
追加“不要博物馆”
→ 新 generation
→ 旧 Packet 被忽略
→ 新 Selection 与 Workspace 无 museum
```

## 场景 10：运行中晚期 supplement

断言：

- 太晚时不假装应用；
- durable command 为 rejected；
- 返回 `requires_new_run`。

## 场景 11：相同输入重复运行

断言：

- Deterministic Mode 结果稳定；
- 不把稳定误判成失败。

## 场景 12：换一套方案

断言：

- Explore Mode 使用不同 Seed；
- 候选有变化；
- 硬要求不变；
- 事实和来源合同不变。

---

# 18. 合同版本与历史数据

当前前后端共同声明：

```text
Delivery Bundle v7
Workspace v7
Research Packet v4
Recommendation Catalog v5
```

前端也镜像了这些版本。

本系列最终版本建议：

```text
IntentSpec v1
ResearchBrief v2
ResearchQueryPlan v1
ResearchPacket v5
RecommendationCatalog v6
CandidateSelection v1
IntentCoverage v1
TripWorkspace v8
DeliveryBundle v8
```

## 18.1 未完成旧 Run

旧 Checkpoint 不应强行升级成新 Intent Contract：

```text
标记 non-resumable
reason = intent_contract_generation_changed
允许从最新输入重新开始
```

## 18.2 已完成历史 Bundle

历史 v7 Bundle 必须保持可查看。

可采用：

```text
服务端按 contract_version 读取已持久化公共快照
或前端支持 v7 / v8 只读联合类型
```

禁止：

```text
用空 IntentCoverage 假装旧 Bundle 已被新 Fidelity Gate 验证
```

旧 Bundle 的状态应是：

```text
legacy_unmeasured
```

而不是：

```text
satisfied
```

## 18.3 合同版本必须真实升级

仓库当前已有明确注释：结构变化必须移动版本号，不能让不同结构共用一个版本。

因此不能只修改字段而继续声称是 v7。

---

# 19. Frontend 次生修改

本轮关注后端，但以下前端类型和展示必须同步：

```text
frontend/src/types/delivery.ts
```

增加：

- PublicFulfillmentSummary；
- EntityIntentExplanation；
- Intent Deviation；
- Explore Mode Run 信息；
- v7/v8 discriminated union。

Plan Gate 应展示：

```text
系统识别到的主要要求
硬要求
偏好
不支持或冲突内容
```

最终行程建议增加一个紧凑区域：

```text
你的要求
- 已满足
- 部分满足
- 无法验证
- 未满足及原因
```

不要把内部分数、Candidate ID 或 Prompt 暴露给普通用户。

---

# 20. 旧代码清理

在本批次结束前删除：

- 旧 JSON String Research Brief 读取；
- `BRIEF_FIELD_PRESETS`；
- LLM Planner Prompt；
- Planner 拓扑归一化；
- 自由文本业务型 `supplemental_requirements`；
- Composer 对全部 admitted candidates 的枚举；
- Admission 即 Placement 的补位语义；
- 未记录 Mutation 的原地修改；
- 无版本 Prompt；
- 确认无调用后删除 `legacy refinement_count`。

不得长期保留：

```text
new_intent_path
legacy_intent_path
```

双路径只能用于短期 Shadow 验证，并且必须有删除时间点。

---

# 21. 建议新增 ADR

```text
docs/adr/ADR-0010-first-class-user-intent-contract.md
docs/adr/ADR-0011-admission-ranking-selection-separation.md
docs/adr/ADR-0012-intent-fidelity-and-controlled-exploration.md
```

分别记录：

1. 为什么 IntentSpec 与 ConstraintPack 分开。
2. 为什么 Truth Admission、Ranking、Selection 分开。
3. 为什么确定性是默认，Explore 是显式模式。
4. 为什么 Intent Fidelity Gate 与 Delivery Quality Gate 分开。
5. 为什么后处理必须有 Mutation Ledger。

---

# 22. 本文档内部实施顺序

## Commit 1：Composition Rules 与 Selected Pool

完成：

- Rule Compiler；
- Composer 只读 Selection Plan；
- `_placement_capabilities` 扩展；
- Prompt/Schema 修改。

## Commit 2：Backfill 与 Mutation Ledger

完成：

- Slot-aware Backfill；
- 禁止 admitted-as-obligation；
- Mutation；
- Mutation 后重验证；
- Authored Fallback 限制。

## Commit 3：Intent Fidelity Gate

完成：

- Coverage；
- Gap；
- Retry Routing；
- Budget；
- Deviation；
- 与 Delivery Quality Gate 串联。

## Commit 4：Projection、Workspace 与前端合同

完成：

- Workspace v8；
- Bundle v8；
- Public Fulfillment；
- Entity Explanation；
- Personalization Influence；
- 历史 v7 处理。

## Commit 5：Explore、Audit、Run Diff 与全量评测

完成：

- SelectionPolicy；
- Seed；
- Run Diff；
- Completion Audit；
- Metrics；
- 行为评测；
- 旧代码清理；
- 文档与 Release Gate。

---

# 23. 最终发布门禁

所有条件必须通过：

## 合同

- IntentSpec 严格且有版本。
- 每个 material clause 可追踪。
- 所有产物 generation 一致。
- v8 合同真实升级。
- 历史 v7 不被误判为已验收。

## 研究

- Intent Query 优先。
- Generic Fallback 有条件。
- 被排除类别不会被查询。
- Query 与 Candidate Lineage 可追踪。

## 候选

- Admission、Ranking、Selection 独立。
- Soft Intent 变化不改变 Truth Admission。
- Selection 变化可解释。
- 通过 Admission 不产生 Placement 义务。

## 行程

- Composer 只消费 Selected Pool。
- 数量、频率、时段、顺序可确定性验证。
- Backfill 不违反 Intent。
- 每次后处理有 Mutation。
- Authored Place 不绕过真实性和 Intent 评估。

## 验收

- Intent Fidelity Gate 独立存在。
- Delivery Quality Gate 保留原职责。
- 硬排除永不违反。
- 无法满足的义务明确降级。
- 软偏好不虚假宣称完全满足。

## 多样性

- Deterministic Mode 可复现。
- Explore Mode 显式。
- 不提高全局 Temperature。
- 不同 Seed 不改变硬合同。

## 可观测性

- Intent、Query、Catalog、Selection、Workspace、Coverage 都有 Hash。
- Prompt、Model、Policy 版本可归因。
- Run Diff 能定位差异在哪一层消失。
- Audit 不保存不必要的原始敏感文本。

## 测试

- Repeatability；
- Sensitivity；
- Explore Diversity；
- Supplement；
- Generation Isolation；
- Provider Truth Regression；
- Contract Migration；
- Frontend Type；
- 全量现有测试。

---

# 24. 三份文档交叉复审

## 24.1 原始问题覆盖矩阵

| 原始问题 | 解决文档 | 最终责任 |
|---|---|---|
| 用户输入没有进入最终结果 | 一、二、三 | IntentSpec → Query → Selection → Fidelity |
| 多次结果近乎完全一致 | 二、三 | Sensitivity Test + Explore Mode |
| Research Brief 压平语义 | 一 | ResearchBriefV2 |
| Planner 伪动态 | 一 | Capability Planner |
| 固定 museum/restaurant/hotel 供给 | 二 | Research Query Plan + Fallback Policy |
| Admission 与 Selection 混淆 | 二 | 三层分离 |
| Composer 看不到主题语义 | 三 | SelectedCandidateCapability |
| Backfill 静默改变用户结果 | 三 | Slot-aware Backfill + Mutation Ledger |
| Quality Gate 不验证用户要求 | 三 | Intent Fidelity Gate |
| Plan Gate 附加内容过晚 | 一 | Amendment Protocol |
| 运行中 supplement 绕过重规划 | 一 | Intent Amendment Router |
| 旧并行 Worker 污染新状态 | 一 | PlanningGeneration |
| 缺少行为评测 | 三 | Metamorphic Evaluation |
| 缺少差异归因 | 三 | Completion Audit + Run Diff |
| 缺少受控多样性 | 三 | SelectionPolicy + Seed |
| 个性化字段已有但无可靠生产者 | 二、三 | Intent Match → Influence |
| 版本与历史 Bundle 风险 | 三 | v8 + legacy_unmeasured |

## 24.2 已检查的次生问题

以下问题已经纳入方案，没有留到实施时临时补：

1. Plan Gate 与 runtime supplement 的统一语义。
2. 并行 Worker 的 stale generation。
3. Candidate 数量因多 Query 爆炸。
4. Provider Cache 与 Intent Cache 的错误耦合。
5. Candidate 重复发现。
6. Research Packet 与 Catalog 版本升级。
7. Workspace 与 Delivery Bundle 版本升级。
8. Frontend 类型同步。
9. 历史 Bundle 可读性。
10. 未完成旧 Checkpoint 的恢复政策。
11. Authored Place 绕过候选管线。
12. 后处理 Mutation 不可观测。
13. 硬排除与义务型硬要求的不同失败政策。
14. 软偏好制造无限修复循环。
15. 动态输出要求导致 Schema 爆炸。
16. Prompt、Model 和 Policy 版本无法归因。
17. CI 调用真实 Provider 的成本风险。
18. Raw User Query 在 Audit 中泄露。
19. 旧 Planner、旧 Brief、旧 supplement 双路径长期残留。
20. 将结果差异误当成质量指标。

## 24.3 明确保留的现有资产

本方案不会推翻：

- ControlledTripIdentity；
- Constraint Pack；
- Tool Gateway；
- Provider Evidence；
- Research Packet 强类型事实合同；
- Candidate Admission；
- Targeted Research Budget；
- Run Deadline；
- Run Budget；
- Typed Workspace；
- Deterministic Projection；
- Delivery Quality Gate；
- Completion Audit；
- Selection Slot；
- Personalization Influence 的投影通路。

## 24.4 明确禁止的错误修复方向

整个实施过程中禁止：

- 增加更多业务 Agent 来掩盖数据合同问题；
- 全局提高 Temperature；
- 只把原始用户输入复制进所有 Prompt；
- 把所有 Intent 塞进 ConstraintPack；
- 让 LLM 判断所有可确定性检查的问题；
- 为了个性化削弱 Provider 真实性；
- 为了填满日程违反用户最大数量；
- 把历史 Bundle 伪装成通过了新 Fidelity Gate；
- 长期保留新旧两套 Planner；
- 用“多次结果不一样”代替意图敏感性指标。

---

# 25. 最终完成定义

只有同时达到下面的结果，P0、P1 和相关 P2 才算真正完成：

```text
同样的输入和事实
    → 稳定、可复现

改变一条有意义的用户要求
    → 相关 Query、Ranking、Selection、Placement 发生可解释变化

所有硬要求
    → 满足，或明确说明无法满足
    → 不允许静默违反

所有软偏好
    → 有真实覆盖记录
    → 不允许只靠报告措辞声称已经考虑

所有最终结果
    → 事实真实
    → 结构合法
    → 用户意图可追踪
    → 后处理可解释
    → 版本可回放
```

这才是 JourneyPilot 从“结构稳定的通用旅行流水线”转变为“意图敏感、事实可信、行为可验证的 Agent 规划系统”的完成标准。
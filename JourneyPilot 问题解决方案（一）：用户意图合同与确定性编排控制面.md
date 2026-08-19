# JourneyPilot 问题解决方案（一）：用户意图合同与确定性编排控制面

## 0. 文档信息

**建议仓库路径**

```text
docs/implementation/agent-orchestration/
01-intent-contract-and-control-plane.md
```

**代码基线**

```text
Dreamaker-TA/JourneyPilot
main@7af0974c00ae0fbd95a75a5407809cd33f37910e
```

**本批次覆盖**

- P0-A：建立完整的用户意图主链。
- P1-A：将伪动态 LLM Planner 重构为确定性 Capability Planner。
- 相关 P2：
  - 状态代际管理；
  - supplement 规范化与重规划；
  - 旧状态字段清理；
  - Prompt、模型和策略版本归因的基础设施。

**本批次暂不处理**

- 具体 Provider 查询策略；
- 候选语义评分；
- 候选选择算法；
- Composer 补位；
- Intent Fidelity Gate；
- Explore Mode。

这些内容分别在文档二和文档三完成。

---

# 1. 本批次为什么必须作为一个整体实施

当前系统的问题不是某个 Prompt 写得不好，而是用户意图没有形成一条能够穿过整个工作流的数据链。

当前 `TravelAgentState` 中存在 `user_query`、`research_brief`、`constraint_pack`、`execution_plan`、`agent_assignments` 和自由文本 `supplemental_requirements`，但没有一等的用户意图合同，也没有记录一条要求由哪个阶段负责、如何验收。

与此同时：

- Research Brief 主要由 `ControlledTripIdentity` 确定性生成，内容仍是通用旅行目标；
- Brief Helper 只按固定字段子集向不同 Worker 投影上下文；
- Planner 只允许四个固定 Agent，随后代码又强制把模型计划归一为固定依赖关系；
- Plan Gate 和运行中的 supplement 可以以自由文本形式进入后续节点，而不一定触发意图重建和依赖失效。   

因此，下面五项不能拆开：

```text
IntentSpec
    ↓
ResearchBriefV2
    ↓
CapabilityPlan
    ↓
AgentAssignmentContract
    ↓
Revision / Invalidation / Supplement Protocol
```

只加 `IntentSpec` 而不改 Planner，意图仍然只会成为背景文本。

只改 Planner 而不加代际机制，运行中修改后旧 Worker 结果仍可能进入新 Catalog。

只修 Plan Gate 而不修 durable supplement，首轮输入会变准，但运行中补充要求仍会绕过新架构。

---

# 2. 当前架构中的具体问题

## 2.1 Research Brief 并不是用户任务合同

当前 Brief 的主要内容是：

```text
目的地
持续天数
旅行风格
出行人员
交通
住宿
预算
通用覆盖维度
```

它适合作为旅行基础背景，但不足以表示：

- “不要博物馆”；
- “每天下午安排一家安静咖啡馆”；
- “每天最多两个主要景点”；
- “重点看当代建筑”；
- “每个地点解释为什么适合摄影”；
- “给两套不同风格方案”。

这些要求分别对应排除、频率、数量、主题、输出字段和方案数量，不是一个普通 `constraints: list[str]` 能稳定承载的。

## 2.2 Constraint Pack 不能替代 IntentSpec

Constraint Pack 当前面向的是个人旅行约束，类别包括预算、饮食、健康、交通、住宿、节奏和目的地偏好等。

它应该继续承担：

- 健康和安全约束；
- 预算上限；
- 交通方式限制；
- 住宿设施要求；
- 老人、儿童、行动能力；
- 长期保存的软偏好。

但它不应该承担：

- 每日数量规则；
- 每日重复安排；
- 主题覆盖；
- 方案数量；
- 输出解释；
- 候选多样性；
- 行程顺序；
- 特定地点必须出现。

正确关系是：

```text
ControlledTripIdentity
    = 去哪、何时、从哪出发

ConstraintPack
    = 旅行者有哪些安全、预算、能力和长期偏好约束

IntentSpec
    = 用户本次具体要求系统完成什么

ResearchBriefV2
    = 将前三者投影成各能力模块可执行的研究与交付简报
```

## 2.3 当前 Planner 是伪动态 Planner

Planner Prompt 只允许四个 Agent，并定义了完整旅行规划的固定结构。代码随后还会：

- 去重 Agent；
- 强制 Destination Researcher 在前；
- 强制 Transport 与 Accommodation 居中；
- 强制 Itinerary Planner 在后；
- 多日行程强制注入完整链路；
- 有住宿夜就强制加入 Accommodation Researcher。 

这意味着模型并没有真正决定执行图，只是在固定图上编写任务文案。

继续优化 Planner Prompt 不值得。正确做法是：

```text
代码决定调用哪些能力、依赖关系和并行关系
LLM 负责理解用户语义和生成研究目标
```

## 2.4 supplement 当前只有“进入 State”，没有“重新建模”

当前 durable supplement 会在节点边界被合并到 `supplemental_requirements`，并按 `command_id` 去重。这保证了要求不重复，但没有保证要求被重新解释为新的 IntentSpec，也没有保证旧研究结果失效。

因此会出现：

```text
旧 IntentSpec / 旧 Planner / 旧查询
                ↓
用户追加“不要博物馆”
                ↓
只把一句自由文本放入下游 Prompt
                ↓
旧候选、旧 Catalog 和旧 Workspace 仍然有效
```

这是必须在本批次一起解决的次生问题。

---

# 3. 目标架构

完成本批次后，规划前半段应变成：

```text
Raw User Query
Preset / Saved Preference / Trip Context
ControlledTripIdentity
            │
            ▼
Request Contract Normalizer
            │
            ├── IntentSpec
            ├── ConstraintPack
            ├── Clause Ledger
            └── Conflict Report
                    │
                    ▼
Deterministic ResearchBriefV2 Builder
                    │
                    ▼
Deterministic Capability Planner
                    │
                    ├── ExecutionPlan
                    └── AgentAssignmentContract
                            │
                            ▼
Plan Gate
                            │
                            ├── approve → seal current generation
                            ├── amend → rebuild request contract
                            └── conflict → require clarification
```

运行中补充要求必须经过：

```text
Durable Supplement Command
            │
            ▼
Intent Amendment Router
            │
            ├── research-affecting
            ├── ranking-affecting
            ├── composition-affecting
            ├── projection-only
            ├── identity-changing
            └── unsupported / too-late
                    │
                    ▼
Generation Invalidation + Correct Re-entry Point
```

---

# 4. 核心合同设计

## 4.1 新增 `IntentSpec`

建议新增：

```text
src/travel_agent/entities/intent_spec.py
```

### 4.1.1 Intent 类型

```python
class IntentStrength(str, Enum):
    HARD = "hard"
    SOFT = "soft"
    INFORMATIONAL = "informational"


class IntentKind(str, Enum):
    OBJECTIVE = "objective"
    MUST_INCLUDE = "must_include"
    MUST_EXCLUDE = "must_exclude"
    THEME = "theme"
    ATTRIBUTE_PREFERENCE = "attribute_preference"
    QUANTITY = "quantity"
    CADENCE = "cadence"
    TIME_WINDOW = "time_window"
    SEQUENCING = "sequencing"
    GEOGRAPHIC = "geographic"
    PACE = "pace"
    ALTERNATIVES = "alternatives"
    OUTPUT_REQUIREMENT = "output_requirement"
    DIVERSITY = "diversity"


class IntentTarget(str, Enum):
    TRIP = "trip"
    VISIT = "visit"
    DINING = "dining"
    LODGING = "lodging"
    LOCAL_TRANSPORT = "local_transport"
    LONG_DISTANCE_TRANSPORT = "long_distance_transport"
    ITINERARY = "itinerary"
    DELIVERY = "delivery"


class VerificationMode(str, Enum):
    DETERMINISTIC = "deterministic"
    SEMANTIC = "semantic"
    MIXED = "mixed"
```

### 4.1.2 不允许使用任意 `Any` 作为 Intent Value

不要设计成：

```python
value: Any
```

否则所有错误都会被延迟到 Composer 或 Fidelity Gate 才暴露。

应使用严格联合类型：

```python
IntentValue = Annotated[
    Union[
        ScalarIntentValue,
        CategoryIntentValue,
        CountIntentValue,
        CadenceIntentValue,
        TimeWindowIntentValue,
        SequenceIntentValue,
        GeographicIntentValue,
        OutputRequirementValue,
        AlternativeIntentValue,
    ],
    Field(discriminator="value_type"),
]
```

例如：

```python
class CountIntentValue(StrictModel):
    value_type: Literal["count"] = "count"
    operator: Literal["at_least", "at_most", "exactly"]
    count: int = Field(ge=0)
    unit: Literal["trip", "day", "destination"]


class CadenceIntentValue(StrictModel):
    value_type: Literal["cadence"] = "cadence"
    frequency: Literal[
        "once_per_trip",
        "once_per_destination",
        "once_per_day",
        "selected_days",
    ]
    count: int = Field(default=1, ge=1)
    time_window: Optional[str] = None
    required_attributes: List[str] = Field(default_factory=list)
```

### 4.1.3 `IntentItem`

```python
class IntentItem(StrictModel):
    intent_id: str

    kind: IntentKind
    target: IntentTarget
    strength: IntentStrength
    priority: int = Field(ge=0, le=100)

    value: IntentValue

    source_kind: Literal[
        "current_request",
        "plan_gate_amendment",
        "run_supplement",
        "preset",
        "saved_preference",
        "trip_context",
        "system_default",
    ]
    source_ref_id: str
    source_text: Optional[str]
    source_span_start: Optional[int]
    source_span_end: Optional[int]

    linked_constraint_ids: List[str]
    verification_mode: VerificationMode
    impact_stages: List[
        Literal[
            "research",
            "admission",
            "ranking",
            "composition",
            "projection",
        ]
    ]

    public_summary: str

    status: Literal[
        "active",
        "superseded",
        "conflicted",
        "unsupported",
    ]
```

### 4.1.4 Intent ID 必须由服务端生成

禁止让模型直接生成最终 `intent_id`。

建议使用：

```text
SHA-256(
  schema_version
  + source_ref_id
  + normalized kind
  + normalized target
  + normalized value
)
```

这样可保证：

- 相同 command 重放不会生成重复 Intent；
- 运行中补充要求能稳定去重；
- Diff 能精确判断哪条要求新增、修改或撤销；
- 模型无法随意伪造责任关联。

### 4.1.5 `IntentSpec`

```python
class IntentSpec(StrictModel):
    schema_version: Literal["journeypilot.intent_spec.v1"]

    intent_spec_id: str
    revision: int
    generation_id: str
    content_hash: str

    active_items: List[IntentItem]
    superseded_items: List[IntentItem]

    conflicts: List[IntentConflict]
    unresolved_clauses: List[UnresolvedClause]

    objective_summary: str
    generated_from_message_ids: List[str]
    generated_from_command_ids: List[str]
```

---

# 5. 增加 Clause Ledger，防止输入在入口阶段丢失

仅有 IntentSpec 仍然不能证明模型是否漏掉了用户输入中的一句话。

因此必须增加：

```python
class ClauseDisposition(str, Enum):
    MAPPED_TO_INTENT = "mapped_to_intent"
    CONTROLLED_IDENTITY = "controlled_identity"
    BACKGROUND_CONTEXT = "background_context"
    NON_ACTIONABLE = "non_actionable"
    UNSUPPORTED = "unsupported"
    UNRESOLVED = "unresolved"


class InputClauseRecord(StrictModel):
    clause_id: str
    source_ref_id: str
    source_text: str
    disposition: ClauseDisposition
    mapped_intent_ids: List[str]
    reason_code: Optional[str]
```

`IntentSpec` 或相邻 `RequestContract` 必须包含完整 `clause_ledger`。

计划批准前必须满足：

```text
每个具有指令语气或约束语气的 clause
    → 至少映射到一条 Intent
    或被明确标记为 unsupported / unresolved
```

不允许静默忽略。

例如：

```text
东京四天，
重点安排当代建筑和街头摄影，
不要博物馆；
每天下午安排一家安静咖啡馆；
每天最多两个主要景点；
每个地点解释为什么适合拍照。
```

至少应拆成：

```text
东京四天
→ controlled identity

重点安排当代建筑
→ theme intent

街头摄影
→ theme intent

不要博物馆
→ must_exclude intent + linked hard constraint

每天下午安排一家安静咖啡馆
→ cadence + time_window + attribute preference

每天最多两个主要景点
→ quantity intent

每个地点解释为什么适合拍照
→ output requirement
```

---

# 6. Request Contract Normalizer

## 6.1 不增加新的业务 Agent

不要再加入一个“Intent Agent”。

实现形式应是一个 Scope 阶段节点和若干纯服务：

```text
src/travel_agent/agents/scope/request_contract_normalizer.py
src/travel_agent/services/intent_normalization.py
src/travel_agent/services/intent_conflicts.py
src/travel_agent/services/intent_revision.py
```

它不是新的可调度 Worker，不进入 Planner Agent 列表。

## 6.2 合并 Intent 与 Constraint 的解析过程

当前 Constraint Normalizer 已经会读取：

- 当前查询；
- 用户档案；
- 手工记忆；
- 语义记忆；
- Preset；
- Controlled Identity。

不要让 Intent Normalizer 和 Constraint Normalizer 分别调用模型解释同一句话，否则会得到两套不一致的解释。

建议将现有 Scope 规范化过程重构为一次严格结构化调用：

```python
class RequestContractNormalizationResult(StrictModel):
    current_request_intents: List[IntentDraft]
    constraint_pack_draft: ConstraintPackDraft
    clause_ledger: List[InputClauseDraft]
    detected_conflicts: List[ConflictDraft]
    unresolved_clauses: List[UnresolvedClauseDraft]
```

服务端随后负责：

- 生成稳定 ID；
- 校验 source span；
- 处理优先级；
- 合并已有 Intent；
- 生成 ConstraintPack；
- 构造正式 IntentSpec；
- 计算 revision 和 hash。

## 6.3 信息优先级

必须将优先级写成唯一政策，不允许每个 Prompt 自己判断：

```text
本轮用户明确要求
    >
本轮 Plan Gate 修改
    >
本轮 durable supplement
    >
ControlledTripIdentity / 用户明确选择的 Preset
    >
已保存的软偏好
    >
历史上下文
    >
系统默认
```

附加规则：

1. 保存的偏好不能自动升级成硬约束。
2. 系统默认不能覆盖当前请求。
3. 当前请求中的明确排除必须覆盖 Preset。
4. ControlledTripIdentity 中的日期和目的地不能被普通自由文本暗中修改。
5. 目的地、日期和出发地的变化必须走 Trip Identity Edit 或新 Run。
6. 冲突不能通过“选一个看起来更合理的”静默解决。

## 6.4 冲突处理

以下冲突必须阻止 Plan Gate 进入可批准状态：

```text
同一对象既 must_include 又 must_exclude
每天最多 2 个景点，同时每天至少 3 个景点
要求全程步行，同时要求老人行动受限且单段不超过 500 米
不住酒店，同时存在多晚异地住宿需求且没有其他住宿形式
```

`IntentConflict` 至少包括：

```python
class IntentConflict(StrictModel):
    conflict_id: str
    intent_ids: List[str]
    conflict_type: Literal[
        "direct_contradiction",
        "quantity_infeasible",
        "identity_conflict",
        "constraint_conflict",
        "unsupported_combination",
    ]
    blocking: bool
    user_visible_summary: str
```

---

# 7. `ResearchBriefV2`

## 7.1 从 JSON 字符串改为严格对象

当前 `research_brief` 是 JSON 字符串，并由固定字段表投影给 Worker。

建议改为：

```text
src/travel_agent/entities/research_brief.py
```

```python
class DomainResearchObjective(StrictModel):
    objective_id: str
    domain: ResearchDomain
    summary: str
    must_cover_intent_ids: List[str]
    optional_intent_ids: List[str]
    excluded_categories: List[str]
    success_criteria: List["SuccessCriterion"]


class ResearchBriefV2(StrictModel):
    schema_version: Literal["journeypilot.research_brief.v2"]

    brief_id: str
    generation_id: str

    controlled_trip_identity_revision: int
    intent_spec_revision: int
    constraint_pack_revision: int

    objective_summary: str
    controlled_trip_identity: ControlledTripIdentity

    domain_objectives: List[DomainResearchObjective]
    delivery_requirements: List[str]

    hard_intent_ids: List[str]
    soft_intent_ids: List[str]

    content_hash: str
```

## 7.2 Brief 必须确定性构造

Intent 和 Constraint 完成规范化后，Brief 不需要再调用模型。

它应由：

```text
ControlledTripIdentity
+ IntentSpec
+ ConstraintPack
+ Product Requirements
```

确定性投影得到。

这样可以同时获得：

- 基础身份不漂移；
- 用户语义不丢失；
- 相同 Request Contract 得到相同 Brief；
- 不增加额外模型不确定性。

## 7.3 删除按固定字段裁剪的旧 Helper

逐步废弃：

```text
utils/brief_helpers.py
BRIEF_FIELD_PRESETS
```

替换为：

```python
build_assignment_context(
    assignment: AgentAssignmentContract,
    brief: ResearchBriefV2,
    intent_spec: IntentSpec,
    constraint_pack: ConstraintPack,
)
```

Worker 不再根据自己的名字从大 Brief 中随意选字段，而是只接收 Assignment 明确授权的内容。

---

# 8. 确定性 Capability Planner

## 8.1 删除 LLM 对图拓扑的决策权

保留节点名称 `planner` 一段时间，以减少 Trace、前端和测试的连锁修改，但其内部改为：

```python
async def planner_node(state: TravelAgentState) -> Dict[str, Any]:
    plan = build_capability_plan(
        identity=state.controlled_trip_identity,
        intent_spec=state.intent_spec,
        constraint_pack=state.constraint_pack,
        brief=state.research_brief,
    )
    return {
        "execution_plan": plan.execution_plan,
        "agent_assignments": plan.agent_assignments,
        "capability_plan": plan,
    }
```

不再调用 Primary LLM 生成执行图。

## 8.2 能力映射

建议唯一能力表：

```python
CAPABILITY_OWNERS = {
    IntentTarget.VISIT: "destination_researcher",
    IntentTarget.DINING: "destination_researcher",
    IntentTarget.LODGING: "accommodation_researcher",
    IntentTarget.LOCAL_TRANSPORT: "transport_researcher",
    IntentTarget.LONG_DISTANCE_TRANSPORT: "transport_researcher",
    IntentTarget.ITINERARY: "itinerary_planner",
    IntentTarget.DELIVERY: "itinerary_planner",
}
```

同时加入产品结构要求：

- 完整旅行规划至少需要 Visit；
- 存在住宿夜则需要 Accommodation；
- 存在跨城或出发返程责任则需要 Transport；
- 最终需要行程则需要 Itinerary Planner。

## 8.3 保留合理的半串行依赖

第一阶段不要贸然重新并行所有 Worker。

建议继续使用：

```text
Destination Researcher
        ↓
Transport Researcher || Accommodation Researcher
        ↓
Candidate / Selection Pipeline
        ↓
Itinerary Planner
```

原因是 Accommodation 仍可能利用目的地区域信息，且当前运行预算、窗口和测试都是围绕这个阶段结构设计的。

真正需要删除的是：

```text
LLM 先生成图
    ↓
代码再把它强行改回固定图
```

## 8.4 `AgentAssignmentContract`

替换当前只有 `task` 和 `recommended_tools` 的 Assignment：

```python
class AgentAssignmentContract(StrictModel):
    assignment_id: str
    generation_id: str

    agent_name: Literal[
        "destination_researcher",
        "transport_researcher",
        "accommodation_researcher",
        "itinerary_planner",
    ]

    objective: str

    must_cover_intent_ids: List[str]
    optional_intent_ids: List[str]

    research_objective_ids: List[str]
    required_candidate_kinds: List[str]
    excluded_categories: List[str]

    success_criteria: List[SuccessCriterion]

    recommended_tools: List[str]
    upstream_assignment_ids: List[str]

    intent_spec_revision: int
    constraint_pack_revision: int
```

Worker 必须能够回答：

```text
我负责哪些 Intent？
成功条件是什么？
哪些类型不允许返回？
我使用的是哪一代 IntentSpec？
```

而不是只收到：

```text
“请研究东京的景点、美食和文化。”
```

---

# 9. Planning Generation 与依赖失效

## 9.1 增加统一代际对象

建议新增：

```python
class PlanningGeneration(StrictModel):
    generation_id: str

    controlled_trip_identity_revision: int
    intent_spec_revision: int
    constraint_pack_revision: int
    plan_revision: int

    identity_hash: str
    intent_hash: str
    constraint_hash: str
```

以下所有产物都必须携带 `generation_id`：

- ResearchBriefV2；
- CapabilityPlan；
- AgentAssignmentContract；
- 后续文档中的 ResearchQueryPlan；
- ResearchPacket；
- RecommendationCatalog；
- CandidateSelectionPlan；
- Workspace；
- IntentCoverageReport；
- DeliveryBundle。

## 9.2 为什么不能只依赖 LangGraph 当前 State

并行 Worker 可能基于旧 State 已经开始执行。用户修改要求后，即使 State 已更新，旧分支仍可能晚于新分支返回。

当前字典 Reducer 使用 last-writer-wins，并明确要求并行分支不能共享同一个业务 key。

因此需要双重保护：

1. Worker 输出 key 包含 generation：

```text
destination_researcher@generation_3@round_1
```

2. Gate 只消费：

```python
packet.generation_id == state.planning_generation.generation_id
```

旧结果可以保留用于审计，但不能进入当前 Catalog。

## 9.3 失效矩阵

| Intent 变化类型 | 保留 | 必须失效 |
|---|---|---|
| 仅投影措辞 | Research、Catalog、Selection、Workspace | Coverage、Projection、Bundle |
| 仅行程数量或时段 | Research、Catalog、Ranking | Selection、Composition、Coverage、Projection |
| 主题或偏好变化 | 已验证事实、Provider Snapshot | Ranking、Selection、Composition、Coverage、Projection |
| 新增排除或候选属性 | Provider Snapshot | 受影响 Domain 的 Research、Catalog、Ranking、后续全部 |
| 健康、预算、交通硬约束 | Controlled Identity | Constraint、Admission、Selection、Composition、后续全部 |
| 日期、目的地、出发地 | 不复用当前 Draft | 新 Planning Generation 或新 Run |

不要把所有修改都粗暴清空，也不要只修改 Prompt 而保留过时 Catalog。

---

# 10. Minimum Delivery Draft 的修改

当前 Minimum Delivery Draft 明确只读取 Controlled Identity 和硬 Constraint，不读取原始用户文本、Planner 或候选。

这个原则应保留，但 Draft 必须同时保存规范化后的硬用户意图。

## 10.1 扩展字段

```python
class MinimumDeliveryDraft:
    ...
    planning_generation_id: str
    intent_spec_revision: int
    intent_spec_hash: str

    preserved_constraint_ids: List[str]
    preserved_hard_intent_ids: List[str]

    user_input_anchors: List[UserInputAnchor]
```

## 10.2 扩展 UserInputAnchor

现有 `UserInputAnchor` 已支持 controlled identity、hard constraint、preference 等输入类型。

建议增加：

```python
input_kind: Literal[
    "controlled_identity",
    "hard_constraint",
    "intent_requirement",
    "preference",
    "fixed_transport",
    "planning_authorization",
]

intent_id: Optional[str]
```

规则：

```text
hard_constraint
    → 必须有 constraint_id

intent_requirement
    → 必须有 intent_id 和 public_summary

其他类型
    → 不得伪造 constraint_id / intent_id
```

Draft 保存的是规范化合同，不保存未经处理的整段用户查询。

---

# 11. Plan Gate 修改协议

## 11.1 禁止 approve 内容直接进入自由文本 supplement

当前 Plan Gate 的 `edit` 和 `supplement` 会重新走约束与 Planner，但 `approve` 携带附加内容时，可以直接进入 `supplemental_requirements`。

新规则：

```text
approve + 空内容
    → 校验 revision
    → seal Draft
    → 开始执行

approve + 非空内容
    → 先解析 Intent Amendment
```

然后根据影响判定：

### 仅展示层变化

例如：

```text
“最终报告尽量简洁。”
```

可以：

```text
应用 amendment
→ 重新生成 delivery requirement
→ 同一次操作批准
```

### 研究、候选或行程变化

例如：

```text
“另外不要博物馆。”
“每天午后安排咖啡馆。”
```

必须：

```text
应用 amendment
→ 新 generation
→ 重建 Brief 与 Capability Plan
→ 向用户显示更新后的计划
→ 重新批准
```

不能在计划已经批准后偷偷改变它的业务含义。

## 11.2 批准前检查

Plan Gate 只有在以下条件全部成立时才允许 seal：

```text
IntentSpec 不存在 blocking conflict
没有未映射的 material clause
每条 active hard intent 有负责能力
每个 research-affecting intent 有 research objective
Minimum Delivery Draft 的 generation 与当前 State 一致
Capability Plan 的 revision 与当前 IntentSpec 一致
```

---

# 12. 运行中 supplement 协议

## 12.1 新增 Intent Amendment Router

建议新增：

```text
src/travel_agent/workflows/intent_amendments.py
src/travel_agent/agents/orchestrator/intent_amendment_router.py
```

不要让任意 Worker 自己解释 supplement。

## 12.2 安全边界

在以下边界检查未处理 supplement：

```text
Planner 前
Dispatcher 前
Candidate Gate 前
Selection 前
Itinerary Planner 前
Intent Fidelity Gate 前
Projection 前
```

## 12.3 Amendment 影响分类

```python
class AmendmentImpact(str, Enum):
    IDENTITY_CHANGE = "identity_change"
    RESEARCH_AFFECTING = "research_affecting"
    ADMISSION_AFFECTING = "admission_affecting"
    RANKING_AFFECTING = "ranking_affecting"
    COMPOSITION_AFFECTING = "composition_affecting"
    PROJECTION_ONLY = "projection_only"
    UNSUPPORTED = "unsupported"
```

## 12.4 阶段处理政策

| Amendment | Research 未开始 | Research 执行中 | Research 已关闭 | Composition 已关闭 |
|---|---:|---:|---:|---:|
| Identity Change | 重建 Draft | 拒绝当前 Run 或重开 | 拒绝 | 拒绝 |
| Research Affecting | 应用并重规划 | 新 generation，旧结果失效 | 拒绝并提示新 Run | 拒绝 |
| Ranking Affecting | 应用 | 应用 | 可重新排名 | 视窗口决定 |
| Composition Affecting | 应用 | 应用 | 重新组合 | 拒绝或新 Run |
| Projection Only | 应用 | 应用 | 应用 | Projection 前仍可应用 |

## 12.5 command 状态必须准确

当前 Run Command 已有：

```text
pending
claimed
consumed
rejected
```

不要新增模糊状态。

使用 `result` 记录：

```json
{
  "outcome": "applied",
  "intent_spec_revision": 4,
  "generation_id": "generation_4"
}
```

或者：

```json
{
  "outcome": "rejected_late",
  "reason_code": "research_window_closed",
  "requires_new_run": true
}
```

只有 Amendment 已经进入 checkpoint，command 才能标记为 `consumed`。

---

# 13. State 修改清单

在 `TravelAgentState` 增加：

```python
request_contract: Optional[RequestContract]
intent_spec: Optional[IntentSpec]
intent_spec_revision: int

planning_generation: Optional[PlanningGeneration]

research_brief: Optional[ResearchBriefV2]
capability_plan: Optional[CapabilityPlan]

pending_intent_amendments: Annotated[
    List[IntentAmendment],
    merge_by_command_id,
]
applied_intent_amendment_ids: List[str]
rejected_intent_amendments: List[IntentAmendmentRejection]

prompt_versions: Dict[str, str]
policy_versions: Dict[str, str]
```

逐步删除或停止业务使用：

```text
research_brief: JSON string
plan_revision: free-form dict
supplemental_requirements: free-text business input
legacy refinement_count
```

`legacy refinement_count` 如果仍有外部 API 或恢复逻辑依赖，应先标记 deprecated，并在文档三统一删除，不要在本批次盲删。

---

# 14. 工作流改造

目标图：

```text
scope_clarifier
    ↓
request_contract_normalizer
    ↓
research_brief_builder
    ↓
minimum_delivery_draft_builder
    ↓
geo_context
    ↓
weather_context
    ↓
capability_planner
    ↓
plan_gate
    ↓
dispatcher
```

替换：

```text
scope_clarifier
→ brief_generator
→ constraint_normalizer
→ LLM planner
```

其中：

- `scope_clarifier` 仍只负责 Controlled Identity；
- `request_contract_normalizer` 统一生成 IntentSpec 与 ConstraintPack；
- `research_brief_builder` 确定性构造 Brief；
- `capability_planner` 确定性构造执行图和 Assignment。

---

# 15. 文件级修改地图

## 新增

```text
src/travel_agent/entities/intent_spec.py
src/travel_agent/entities/request_contract.py
src/travel_agent/entities/research_brief.py
src/travel_agent/entities/capability_plan.py
src/travel_agent/entities/planning_generation.py

src/travel_agent/services/intent_normalization.py
src/travel_agent/services/intent_conflicts.py
src/travel_agent/services/intent_revision.py
src/travel_agent/services/capability_planning.py
src/travel_agent/services/state_invalidation.py

src/travel_agent/agents/scope/request_contract_normalizer.py
src/travel_agent/agents/orchestrator/intent_amendment_router.py

src/travel_agent/workflows/intent_amendments.py
```

## 重点修改

```text
src/travel_agent/entities/state.py
src/travel_agent/entities/delivery_bundle.py
src/travel_agent/agents/scope/node.py
src/travel_agent/agents/scope/constraint_normalizer.py
src/travel_agent/agents/orchestrator/planner.py
src/travel_agent/agents/orchestrator/prompts.py
src/travel_agent/workflows/travel_planning.py
src/travel_agent/workflows/minimum_delivery_draft.py
src/travel_agent/workflows/run_control.py
src/travel_agent/utils/brief_helpers.py
src/travel_agent/services/product_requirements.py
frontend/src/types/delivery.ts
```

---

# 16. 本文档内部实施顺序

这是一轮开发批次，不是五轮独立任务。

## Commit 1：合同与不变量

完成：

- IntentSpec；
- Clause Ledger；
- RequestContract；
- PlanningGeneration；
- 严格验证；
- Stable ID 和 Hash；
- 单元测试。

此时不接入工作流。

## Commit 2：统一 Request Contract Normalizer

完成：

- 现有 Constraint Normalizer 迁移；
- 单次结构化解析；
- 来源优先级；
- 冲突；
- Clause Coverage；
- Intent 与 Constraint 关联。

## Commit 3：ResearchBriefV2 与 Capability Planner

完成：

- typed brief；
- 确定性 Planner；
- AgentAssignmentContract；
- 删除 Planner LLM 拓扑调用；
- 保留 Trace 节点兼容性。

## Commit 4：Plan Gate、supplement 与 generation invalidation

完成：

- Plan Gate Amendment；
- 运行中 supplement；
- stale generation 拒绝；
- state invalidation；
- Draft seal 条件。

## Commit 5：文档、版本、全量测试

完成：

- ADR；
- invariants；
- architecture overview；
- release checklist；
- frontend 类型同步；
- 全量回归。

---

# 17. 必须新增的不变量

写入：

```text
docs/invariants.md
```

仓库当前明确要求每条不变量必须有 Owner 和测试，修复过的事故必须永久变成测试。

建议新增：

## INV-INTENT-001：所有当前指令都必须被归类

```text
Owner:
request_contract_normalizer

Enforced by:
material clause 必须映射到 intent 或明确 unresolved/unsupported

Tests:
tests/agent_behavior/test_intent_clause_coverage.py
```

## INV-INTENT-002：每条 active hard intent 必须有责任能力

```text
Owner:
capability_planning.py

Tests:
tests/agent_behavior/test_capability_plan.py
```

## INV-INTENT-003：旧 generation 的产物不得进入当前 Catalog

```text
Owner:
state_invalidation.py
candidate_gate.py

Tests:
tests/agent_behavior/test_generation_isolation.py
```

## INV-INTENT-004：supplement 不得绕过 Request Contract Normalizer

```text
Owner:
intent_amendments.py
run_control.py

Tests:
tests/agent_behavior/test_intent_amendments.py
```

## INV-PLAN-001：相同 Request Contract 得到相同执行图

```text
Owner:
capability_planning.py

Tests:
tests/agent_behavior/test_capability_plan.py
```

---

# 18. 测试计划

## 18.1 Intent 提取

至少覆盖：

```text
主题
排除
必须包含
每日数量
每日频率
时段
顺序
住宿属性
交通限制
输出解释
多套方案
混合中英文
否定句
补充修改
```

## 18.2 Clause Coverage

断言：

- 用户每个具有执行意义的子句都有 disposition；
- 不允许 material clause 无记录消失；
- unresolved 会阻止批准。

## 18.3 冲突

覆盖：

- include/exclude 同一地点；
- at_most 与 at_least 冲突；
- 当前请求与 Preset 冲突；
- 当前请求与 saved preference 冲突；
- identity 修改。

## 18.4 Capability Plan

断言：

- 同样 IntentSpec 得到相同图；
- 不再调用 LLM 决定拓扑；
- 只有需要的 Worker 被加入；
- 每个 Intent 有 owner；
- Itinerary Planner 不承担研究职责。

## 18.5 Plan Gate

断言：

- 空 approve 可以 seal；
- material approve content 触发重规划和重新批准；
- presentation-only amendment 可以直接应用；
- conflict 不能批准；
- Draft generation 必须匹配。

## 18.6 Runtime Supplement

断言：

- 同一 command 只应用一次；
- supplement 被解析成 Intent；
- research-affecting supplement 使旧 Packet 失效；
- 太晚的 supplement 被明确拒绝；
- command 不会被错误标记为已应用。

## 18.7 Generation

断言：

```text
旧 Worker 晚返回
    ≠
覆盖新 generation
```

并在正反 merge 顺序下都验证。

---

# 19. 兼容与迁移

## 19.1 Checkpoint

新代码不得尝试把不完整旧 State 猜成新的 IntentSpec。

部署时应明确选择：

```text
未完成的旧 generation Run
    → 标记 NON_RESUMABLE
    → reason_code = intent_contract_generation_changed
    → 允许用户从最新输入重新启动
```

不要保留一套长期双读逻辑。

## 19.2 已完成 Bundle

本批次暂不修改最终公开 Bundle 的意图覆盖结构，正式 Bundle 版本升级放到文档三。

## 19.3 临时兼容字段

允许短期保留：

```text
research_brief legacy serializer
agent_assignment.task
supplemental_requirements read-only
```

但必须：

- 明确标注 deprecated；
- 禁止新业务逻辑写入；
- 在文档三删除；
- 设置测试防止新调用点增加。

---

# 20. 本批次验收标准

本批次完成后必须满足：

1. 当前用户输入被解析成稳定、严格、可版本化的 IntentSpec。
2. 每个 material clause 有处理记录。
3. ConstraintPack 与 IntentSpec 分工明确。
4. ResearchBrief 不再只由 Controlled Identity 决定。
5. Planner 不再使用 LLM 决定执行图。
6. 每个 Agent Assignment 都携带责任 Intent 和成功标准。
7. Plan Gate 的附加内容不能绕过规范化。
8. 运行中 supplement 不能绕过规范化。
9. 所有主要产物携带 generation。
10. 旧 generation 的并行结果不能污染当前 Run。
11. Minimum Delivery Draft 保存硬 Intent 合同。
12. 所有新不变量都有测试。
13. 原有身份、预算、Deadline、Run Budget 和真实性合同不得弱化。

---

# 21. 本批次停止条件

以下任一情况仍存在，禁止开始文档二：

- Worker 仍只能看到一段无责任 ID 的自由文本任务；
- Planner 仍调用 LLM 决定四 Agent 拓扑；
- approve 内容仍直接追加到 `supplemental_requirements`；
- runtime supplement 仍然只是 Prompt 注入；
- IntentSpec 修改后旧 Packet 仍可能被 Candidate Gate 接收；
- 某条 material clause 可以在无状态记录的情况下消失；
- Minimum Delivery Draft 不知道当前硬 Intent；
- 相同 Request Contract 无法稳定复现相同 Capability Plan。
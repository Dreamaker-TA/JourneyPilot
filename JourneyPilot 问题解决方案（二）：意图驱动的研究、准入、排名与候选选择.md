# JourneyPilot 问题解决方案（二）：意图驱动的研究、准入、排名与候选选择

## 0. 文档信息

**建议仓库路径**

```text
docs/implementation/agent-orchestration/
02-intent-aware-research-ranking-selection.md
```

**前置条件**

文档一已经全部完成，并且系统中已经存在：

- IntentSpec；
- PlanningGeneration；
- ResearchBriefV2；
- CapabilityPlan；
- AgentAssignmentContract；
- supplement Amendment Protocol；
- generation isolation。

**本批次覆盖**

- P0-B：修复候选发现、候选供给和候选选择。
- Admission 与 Ranking/Selection 分层。
- 意图驱动 Research Query Plan。
- Candidate Gate 的意图缺口和定向补研。
- 相关 P1：候选行为可解释性和候选差异归因。

---

# 1. 本批次解决的核心问题

当前候选链路可以概括为：

```text
通用任务描述
    ↓
通用 RAG / Provider 查询
    ↓
真实候选
    ↓
预算、天气、约束适配
    ↓
通过准入
    ↓
进入 Composer
```

它的问题不在“候选不真实”，而在于“候选为什么适合当前用户”没有成为一等决策。

Destination Researcher 当前 RAG 查询优先使用 `task_desc or user_query`。由于 Planner 通常会生成通用 `task_desc`，RAG 很容易继续围绕通用旅行任务检索，而不是围绕用户具体主题。

确定性预检中还存在：

```text
museum in <destination>
restaurant in <destination>
hotel in <destination>
```

这些查询是基于 Provider 命中率实验选择的，工程依据充分，但目前被当成默认候选供给，而不是缺货时的兜底。 

Candidate Admission 当前附加的适配分数只有：

```text
budget_fit
weather_fit
constraint_fit
```

没有主题、意图、区域、多样性或候选来源类型。

本批次的目标是把链路改成：

```text
IntentSpec
    ↓
Research Query Plan
    ↓
Intent-first Research
    ↓
Truth Admission
    ↓
Candidate Intent Evaluation
    ↓
Ranking
    ↓
Set Selection
    ↓
CandidateSelectionPlan
```

---

# 2. 三个概念必须彻底分开

## 2.1 Truth Admission

回答：

> 这个候选是否真实、合法、可被系统采用？

继续由当前 Candidate Admission 负责：

- Provider 身份；
- 地址与国家；
- 来源；
- Fact Assertion；
- 预算；
- 天气；
- 硬约束；
- 时间、价格和路线合同。

## 2.2 Intent Ranking

回答：

> 在所有真实可用的候选中，它对当前用户有多合适？

新服务负责：

- 主题匹配；
- 必须包含；
- 排除；
- 属性偏好；
- 区域偏好；
- 节奏；
- 多样性；
- 候选是否来自通用兜底；
- 证据充分度。

## 2.3 Candidate Selection

回答：

> 本次行程应该选哪些候选进入 Composer 的可用池？

它是一个组合问题，需要考虑：

- Intent 覆盖；
- 行程天数；
- 每日数量；
- 每日频率；
- 目的地区域；
- 重复性；
- 交通成本；
- 备选数量；
- 可验证性。

不能再使用：

```text
通过 Admission
≈
应该进入行程
```

---

# 3. Research Query Plan

建议新增：

```text
src/travel_agent/entities/research_query_plan.py
src/travel_agent/services/research_query_planner.py
```

## 3.1 Query 合同

```python
class ResearchQueryKind(str, Enum):
    INTENT_PRIMARY = "intent_primary"
    STRUCTURAL = "structural"
    EVIDENCE_ENRICHMENT = "evidence_enrichment"
    GENERIC_FALLBACK = "generic_fallback"
    TARGETED_REPAIR = "targeted_repair"


class ResearchQuery(StrictModel):
    query_id: str
    generation_id: str

    domain: ResearchDomain
    destination_id: str

    query_kind: ResearchQueryKind
    query_text: str
    aliases: List[str]

    intent_ids: List[str]
    excluded_categories: List[str]

    desired_candidate_count: int
    provider_route: Literal[
        "rag",
        "web_discovery",
        "global_place_search",
        "amap_place_search",
        "rail_provider",
        "route_provider",
        "mixed",
    ]

    priority: int
    fallback_after_query_ids: List[str]
```

```python
class ResearchQueryPlan(StrictModel):
    schema_version: Literal["journeypilot.research_query_plan.v1"]

    query_plan_id: str
    generation_id: str
    intent_spec_revision: int

    queries: List[ResearchQuery]
    per_domain_candidate_caps: Dict[str, int]

    policy_version: str
    content_hash: str
```

## 3.2 Query 生成责任

LLM 可以用于生成主题查询变体，但不能：

- 决定执行图；
- 生成最终 Query ID；
- 绕过排除类别；
- 修改 Provider 路由政策；
- 任意增加查询数量；
- 把泛化查询伪装成 Intent Query。

建议：

```text
代码生成 Query Skeleton
    ↓
Fast Model 为模糊主题生成 1～2 个检索短语
    ↓
服务端校验、去重、绑定 Intent、生成 ID
```

## 3.3 Query 分层

### 第一层：Intent Primary

例如：

```text
Tokyo contemporary architecture photography locations
Tokyo quiet architectural cafe afternoon
```

### 第二层：Structural

满足产品基础合同：

```text
必须有 Visit
存在住宿夜则必须有 Lodging
存在跨城责任则必须有 Transport
```

当前 `product_requirements.py` 已经明确把 Visit 作为结构性产品承诺，并区分发现域与强制交付域。这个设计应保留，但不能替代具体 Intent。

### 第三层：Generic Fallback

只有在前两层没有找到足够真实候选时执行。

### 第四层：Targeted Repair

由 Candidate Gate 针对特定 Intent 缺口发起。

---

# 4. 通用查询降级为 Fallback

## 4.1 不直接删除现有公式

现有 `museum in`、`restaurant in` 和 `hotel in` 是为 Provider 命中率设计的，不应粗暴删除。

正确做法是加入：

```python
class FallbackQueryPolicy:
    policy_version: str
    provider_specific_templates: ...
    forbidden_when_intent_categories_excluded: ...
    fallback_penalty: float
```

## 4.2 执行条件

Generic Fallback 只能在以下条件同时满足时执行：

```text
Intent Primary 已执行
Structural Query 已执行
真实候选数量仍小于结构需求
当前 Intent 未禁止该类别
Research Window 仍开放
Run Budget 仍允许
```

## 4.3 “不要博物馆”时的行为

禁止出现：

```text
用户：不要博物馆
系统：仍然执行 museum in Tokyo
```

应在 Provider 调用前完成排除：

```python
if "museum" in assignment.excluded_categories:
    disable_query_template("museum")
```

如果没有其他可验证 Visit 候选：

- 使用其他允许的 Provider Category；
- 先 Web Discovery，再解析具体地点身份；
- 最终候选不足时记录缺口；
- 不得用博物馆强行补齐。

## 4.4 Fallback 候选必须有显式来源标签

```python
class CandidateDiscoveryOrigin(str, Enum):
    INTENT_QUERY = "intent_query"
    STRUCTURAL_QUERY = "structural_query"
    GENERIC_FALLBACK = "generic_fallback"
    TARGETED_REPAIR = "targeted_repair"
    COMPOSER_AUTHORED_FALLBACK = "composer_authored_fallback"
```

---

# 5. Candidate Discovery Lineage

不要让模型自己声明“这个候选满足哪个 Intent”。

建议新增服务端生成的：

```python
class CandidateDiscoveryRecord(StrictModel):
    candidate_id: str
    generation_id: str

    query_ids: List[str]
    intent_ids: List[str]
    origins: List[CandidateDiscoveryOrigin]

    provider_audit_ids: List[str]
    discovered_at_rounds: List[int]
```

当同一 Provider Place ID 被多个 Query 找到时：

```text
不生成多个 Candidate
    ↓
按稳定身份去重
    ↓
合并 query_ids / intent_ids / origins
```

这样可以表达：

```text
同一个咖啡馆同时覆盖
- 安静咖啡馆
- 建筑摄影
- 下午休息
```

## 5.1 ResearchPacket 元数据

当前 ResearchPacket 已有 `constraint_pack_revision`、`fact_data_revision` 和自由结构 `query_context`。

升级后增加：

```text
generation_id
intent_spec_revision
research_query_plan_id
executed_query_ids
candidate_discovery_records
```

必须升级：

```text
journeypilot.research_packet.v4
→ journeypilot.research_packet.v5
```

## 5.2 服务端所有权

`research_packet_output.py` 当前已经采用大量服务端绑定逻辑，避免模型伪造 Provider 身份和元数据。

Intent 和 Query Lineage 必须延续同一原则：

- 模型可以输出推荐理由草稿；
- 模型不能生成正式 `intent_id`；
- 模型不能生成正式 `query_id`；
- 模型不能声明已满足某 Intent；
- 正式绑定由 Packet Compiler 根据执行记录完成。

---

# 6. Worker 改造

## 6.1 Destination Researcher

### 当前问题

- RAG 查询容易被通用 `task_desc` 控制；
- 海外 Visit 默认预检锚定 museum；
- Dining 默认预检锚定 restaurant；
- Query 与用户具体 Intent 没有正式绑定。

### 修改

```text
resolve_agent_assignment
    ↓
读取 assignment.research_query_ids
    ↓
从 ResearchQueryPlan 获取 Query
    ↓
逐个或按兼容批次执行
    ↓
候选按 Provider Identity 去重
    ↓
生成 CandidateDiscoveryRecord
```

RAG 调用必须从：

```python
_run_rag(retriever, task_desc or user_query, corpus)
```

改为：

```python
_run_rag(
    retriever,
    query.query_text,
    corpus,
    intent_ids=query.intent_ids,
)
```

### 查询预算

不能让每条 Intent 都无限扩张工具调用。

建议：

```text
每个 destination/domain：
- 最多 2 条 Intent Primary
- 最多 1 条 Structural
- 最多 1 条 Generic Fallback
- Targeted Repair 独立受 Gate Budget 控制
```

实际数量写入版本化政策，不写死在多个 Worker。

## 6.2 Accommodation Researcher

现有住宿确定性身份绑定必须保留，因为它能保证至少获得真实酒店身份。

但要拆开两个判断：

```text
找到真实酒店
    ≠
住宿偏好研究完成
```

例如：

```text
安静住宅区
设计酒店
靠近某片区
适合摄影
有公共空间
地铁站附近
```

处理流程应是：

```text
Intent Query 发现具体属性相关酒店
    ↓
Provider Identity Resolution
    ↓
Web / RAG Enrichment
    ↓
Truth Admission
    ↓
Intent Ranking
```

只有 Intent Query 没找到足够候选时，才执行：

```text
hotel in <destination>
```

作为通用身份供给。

## 6.3 Transport Researcher

Transport 的 Provider 证据和精确日期责任不能削弱。

新增内容主要是：

- `must_cover_intent_ids`；
- 出发时段；
- 交通方式排除；
- 最大换乘；
- 夜间交通；
- 舒适度；
- 长途与本地交通分域；
- Intent 与具体候选的绑定。

Transport 不需要追求随机多样性。

## 6.4 Itinerary Planner

本批次只改变其输入接口：

```text
不再直接消费全部 admitted candidates
改为等待 CandidateSelectionPlan
```

具体 Composer 改造在文档三实施。

---

# 7. Hard Intent 与 Admission 的关系

“Admission 只负责真实性”不能被误解成“硬用户要求也要等到 Ranking 才处理”。

以下要求应同步投影到 ConstraintPack，并由 Admission 阻止违反候选：

```text
不要博物馆
不能爬楼
禁止飞机
必须无障碍
预算不能超过上限
食物过敏
必须有电梯
```

需要扩展 Constraint Category：

```text
candidate_category_exclusion
candidate_attribute_requirement
candidate_attribute_exclusion
```

IntentItem 通过：

```python
linked_constraint_ids
```

关联到 ConstraintPack。

但以下要求不属于 Admission：

```text
当代建筑优先
更适合摄影
每天一家咖啡馆
给两套方案
每天最多两个景点
必须包含某个地点
```

它们分别属于：

- Ranking；
- Selection；
- Composition；
- Projection；
- Fidelity。

---

# 8. Candidate Intent Evaluation

建议新增：

```text
src/travel_agent/entities/candidate_intent.py
src/travel_agent/services/candidate_intent_evaluation.py
```

```python
class IntentMatchStatus(str, Enum):
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    VIOLATED = "violated"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class CandidateIntentMatch(StrictModel):
    candidate_id: str
    intent_id: str

    status: IntentMatchStatus
    score: Optional[float]

    method: Literal[
        "deterministic",
        "semantic_batch_evaluation",
    ]

    supporting_fact_assertion_ids: List[str]
    supporting_source_record_ids: List[str]

    reason_code: str
    public_reason: Optional[str]
```

## 8.1 确定性评估

优先处理：

- Provider Category；
- Visit Type；
- Meal Type；
- Facility；
- 价格；
- 地址；
- 距离；
- 时间；
- Country；
- Transport Mode；
- Reservation；
- 已验证标签。

## 8.2 语义评估

仅用于：

- 当代建筑；
- 安静；
- 当地生活感；
- 适合摄影；
- 小众；
- 亲子教育意义；
- 浪漫；
- 设计感。

语义评估必须：

1. 按 Domain 批量执行，不逐候选调用。
2. 使用 Fast Model 和严格 JSON Schema。
3. 只读取候选已有事实、来源摘要和支持文本。
4. 没有证据时输出 `unknown`。
5. 不允许为获得高匹配分数而编造候选属性。
6. 缓存键包含：

```text
intent_hash
candidate_fact_fingerprint
evaluation_policy_version
model_version
prompt_version
```

---

# 9. Candidate Ranking

建议新增：

```text
src/travel_agent/entities/candidate_ranking.py
src/travel_agent/services/candidate_ranking.py
```

## 9.1 不使用可补偿的单一加权总分作为第一原则

以下情况不允许：

```text
违反硬排除
+
主题匹配分高
=
仍然排名靠前
```

应使用分层排序：

```text
第一层：硬规则是否允许
第二层：是否匹配 must_include / 高优先级 Intent
第三层：新覆盖多少尚未覆盖的高优先级 Intent
第四层：主题和属性匹配
第五层：预算、天气、Constraint Fit
第六层：证据充分度
第七层：区域一致性和多样性
第八层：Generic Fallback、重复和交通成本惩罚
第九层：稳定 Candidate ID 作为确定性 tie-break
```

## 9.2 Ranking Score

```python
class CandidateRankingScore(StrictModel):
    candidate_id: str
    generation_id: str

    hard_eligible: bool
    hard_violation_intent_ids: List[str]

    matched_intent_ids: List[str]
    unknown_intent_ids: List[str]

    high_priority_coverage_score: float
    semantic_fit: float
    evidence_confidence: float

    budget_fit: float
    weather_fit: float
    constraint_fit: float

    regional_fit: float
    diversity_potential: float

    generic_fallback_penalty: float
    redundancy_penalty: float
    travel_cost_penalty: float

    ranking_tuple: List[float]
    policy_version: str
```

当前 Admission 的三个 Fit 不删除，而是成为 Ranking 的输入。

---

# 10. Candidate Selection

建议新增：

```text
src/travel_agent/entities/candidate_selection.py
src/travel_agent/services/candidate_selection.py
```

```python
class CandidateSelectionRole(str, Enum):
    REQUIRED_PRIMARY = "required_primary"
    PRIMARY = "primary"
    ALTERNATIVE = "alternative"
    FALLBACK = "fallback"


class CandidateSelectionEntry(StrictModel):
    candidate_id: str
    domain: ResearchDomain
    destination_id: str

    role: CandidateSelectionRole
    rank: int

    covered_intent_ids: List[str]
    selection_reasons: List[str]

    eligible_for_composition: bool


class CandidateSelectionPlan(StrictModel):
    schema_version: Literal["journeypilot.candidate_selection.v1"]

    selection_plan_id: str
    generation_id: str

    intent_spec_revision: int
    catalog_revision: int

    entries: List[CandidateSelectionEntry]

    covered_intent_ids: List[str]
    uncovered_intent_ids: List[str]

    policy_version: str
    selection_seed: Optional[int]
    content_hash: str
```

## 10.1 选择顺序

### 第一步：锁定必须候选

处理：

- 用户明确必须包含的地点；
- 长途交通责任；
- 每个住宿区间必须有的住宿；
- 固定交通；
- 已选中的 Selection Slot。

### 第二步：覆盖高优先级 Intent

使用确定性贪心 Set Cover：

```text
选择能覆盖最多尚未覆盖高优先级 Intent 的候选
```

### 第三步：满足结构需求

根据：

- 行程天数；
- 每日最大数量；
- 每日频率；
- 目的地数量；
- 住宿区间；
- 备选数量；

计算需要多少候选。

### 第四步：增加多样性和备选

使用 MMR 或确定性多样性选择：

```text
高相关
但不过度重复
```

## 10.2 关键原则

通过 Admission 的候选只代表：

```text
可以被选择
```

不代表：

```text
必须被选择
必须进入 Composer
必须得到一天的 Placement
```

---

# 11. Candidate Gate 改造

## 11.1 保留现有职责

Candidate Gate 继续唯一负责：

- Candidate Admission；
- Provider Evidence；
- 定向补研预算；
- Gap Attempt；
- Research Window；
- Catalog 构造。

## 11.2 新增 Intent Gap

扩展 `CandidateResearchGap.reason`：

```text
missing_intent_candidate
insufficient_intent_evidence
selection_coverage_gap
```

增加字段：

```python
intent_id: Optional[str]
query_id: Optional[str]
generation_id: str
desired_candidate_count: Optional[int]
```

## 11.3 定向补研规则

例如：

```json
{
  "reason": "missing_intent_candidate",
  "intent_id": "i_daily_quiet_cafe",
  "research_domain": "dining",
  "destination_id": "tokyo",
  "desired_candidate_count": 2
}
```

Candidate Gate 应创建：

```text
TARGETED_REPAIR ResearchQuery
```

而不是重新发送：

```text
“请补充更多餐饮候选”
```

## 11.4 预算耗尽

Research Budget 耗尽后：

- 不得把 Generic Candidate 伪装为满足主题；
- 不得把 `unknown` 改成 `matched`；
- 记录 uncovered intent；
- 交给文档三的 Intent Fidelity Gate 形成明确 deviation。

---

# 12. Recommendation Catalog 修改

升级：

```text
journeypilot.recommendation_catalog.v5
→ journeypilot.recommendation_catalog.v6
```

新增：

```python
generation_id: str
intent_spec_revision: int
research_query_plan_id: str

candidate_discovery_records: List[CandidateDiscoveryRecord]
candidate_intent_matches: List[CandidateIntentMatch]
candidate_ranking_scores: List[CandidateRankingScore]
```

`CandidateSelectionPlan` 建议仍作为独立 State 产物，不直接塞入 Catalog，因为：

```text
Catalog
    = 已研究、已准入、已评估的候选事实

SelectionPlan
    = 本次运行如何从 Catalog 中选择
```

这两个生命周期不同。

---

# 13. 现有 PersonalizationInfluence 的正确定位

仓库已经有：

- `PersonalizationInfluence`；
- Candidate 和 Entity Lineage 中的 `personalization_influence_ids`；
- Workspace 中的 `personalization_influences`；
- Projection 传递这些 ID。 

这套结构不应该删除，但它不是 IntentSpec 的替代品。

正确关系：

```text
IntentSpec
    = 用户要求的原始业务合同

CandidateIntentMatch
    = 候选与 Intent 的评估

CandidateSelectionPlan
    = 选择决策

PersonalizationInfluence
    = 最终对用户解释“这条要求如何影响了这个实体”
```

建议修改：

```python
class PersonalizationInfluence:
    influence_id: str
    target_ref: ...

    intent_id: str
    constraint_id: Optional[str]

    effect: Literal[
        "candidate_filter",
        "option_ranking",
        "selection_reason",
        "schedule_rule",
        "output_requirement",
    ]

    source_kind: ...
    display_text: str
```

正式 Influence 应在 Selection 或 Composition 完成后由服务端生成，不由 Worker 模型自由生成。

---

# 14. 缓存政策

## 14.1 Provider Snapshot Cache

保持按真实 Provider 请求参数缓存。

不要为了 Intent 变化强制禁用 Provider Cache。

例如：

```text
相同 hotel in Tokyo 请求
```

无论用户偏好如何，真实 Provider 结果都可以复用。

## 14.2 不把 Intent Hash 放进 Provider Cache Key

否则相同事实会因不同用户偏好重复请求 Provider。

## 14.3 Intent Evaluation Cache

新增单独缓存：

```text
intent_hash
+ candidate_fact_fingerprint
+ evaluation_policy_version
+ model_version
+ prompt_version
```

事实缓存和语义评估缓存必须分开。

---

# 15. 候选数量与 Token 风险

当前 Research Packet 已根据真实运行测量设置候选数量上限，例如 Destination 每类 4 个、Accommodation 4 个。

引入多 Query 后不能变成：

```text
每条 Query 各返回 4 个
```

否则 Prompt 和 Catalog 会爆炸。

必须采用：

```text
Query 级期望数量
+
Domain 全局上限
+
Provider Identity 去重
```

例如：

```text
Visit Domain 总上限：8
Intent Query A：期望 3
Intent Query B：期望 3
Structural Query：期望 2
Generic Fallback：只有总数不足时补到上限
```

同一个 Candidate 被多个 Query 命中不重复计数。

---

# 16. 文件级修改地图

## 新增

```text
src/travel_agent/entities/research_query_plan.py
src/travel_agent/entities/candidate_discovery.py
src/travel_agent/entities/candidate_intent.py
src/travel_agent/entities/candidate_ranking.py
src/travel_agent/entities/candidate_selection.py

src/travel_agent/services/research_query_planner.py
src/travel_agent/services/candidate_intent_evaluation.py
src/travel_agent/services/candidate_ranking.py
src/travel_agent/services/candidate_selection.py
src/travel_agent/services/fallback_query_policy.py
```

## 修改

```text
src/travel_agent/entities/state.py
src/travel_agent/entities/delivery_bundle.py
src/travel_agent/agents/research_packet_output.py

src/travel_agent/agents/destination_researcher/node.py
src/travel_agent/agents/accommodation_researcher/node.py
src/travel_agent/agents/transport_researcher/node.py

src/travel_agent/agents/orchestrator/candidate_gate.py
src/travel_agent/services/candidate_admission.py
src/travel_agent/services/product_requirements.py

src/travel_agent/workflows/travel_planning.py
frontend/src/types/delivery.ts
```

---

# 17. 本文档内部实施顺序

## Commit 1：Research Query Plan 与 Discovery Lineage

完成：

- Query 合同；
- Query Planner；
- Query ID；
- Candidate Discovery Record；
- generation/revision；
- 单元测试。

## Commit 2：Worker Query 执行改造

完成：

- Destination；
- Accommodation；
- Transport；
- RAG 读取 Query；
- Generic Fallback 条件化；
- Provider Identity 去重。

## Commit 3：Intent Evaluation 与 Ranking

完成：

- 确定性匹配；
- 批量语义评估；
- Ranking Tuple；
- 评估缓存；
- 证据约束。

## Commit 4：Candidate Selection 与 Intent Gap

完成：

- Selection Plan；
- Set Cover；
- Alternatives；
- Candidate Gate 定向补研；
- uncovered intent。

## Commit 5：合同版本、文档和全量测试

完成：

- Research Packet v5；
- Catalog v6；
- frontend 类型；
- ADR；
- invariants；
- 回归。

---

# 18. 必须新增的不变量

## INV-RESEARCH-001：Intent Query 先于 Generic Fallback

```text
Owner:
research_query_planner.py
fallback_query_policy.py
```

## INV-RESEARCH-002：被明确排除的类别不得进入 Provider Query

```text
Owner:
research_query_planner.py
worker query executor
```

## INV-CANDIDATE-001：Admission 与 Ranking 不得互相替代

```text
Owner:
candidate_admission.py
candidate_ranking.py
```

## INV-CANDIDATE-002：模型不得生成正式 Intent/Query Lineage

```text
Owner:
research_packet_output.py
```

## INV-CANDIDATE-003：通过 Admission 不产生 Placement 义务

```text
Owner:
candidate_selection.py
itinerary_planner.py
```

## INV-CANDIDATE-004：相同 Identity Candidate 在多个 Query 中只出现一次

```text
Owner:
candidate discovery deduplicator
```

---

# 19. 行为测试

建议新建：

```text
tests/agent_behavior/
tests/agent_behavior/fixtures/
```

## 19.1 主题变化

固定东京四天。

A：

```text
传统寺庙庭园
```

B：

```text
当代建筑
```

断言：

- Identity 相同；
- Query Plan 不同；
- Provider 事实可以部分复用；
- Candidate Intent Match 不同；
- Selection Plan 存在显著差异。

## 19.2 排除博物馆

断言：

- 不执行 museum fallback；
- Admission 或硬筛选中不允许 museum；
- Selection Plan 中没有 museum；
- uncovered 时记录缺口，不强行补位。

## 19.3 住宿偏好

A：

```text
住交通枢纽附近
```

B：

```text
住安静住宅区的设计酒店
```

断言：

- Query 不同；
- 同一通用酒店 Provider Snapshot 可以复用；
- Ranking 和 Selection 不同。

## 19.4 Admission 与 Ranking 解耦

改变软主题后：

```text
Admission Result 应保持一致
Candidate Intent Match 和 Ranking 应变化
```

## 19.5 Generic Fallback

断言：

- Intent Query 足够时不执行；
- 候选不足时执行；
- Fallback Candidate 有明确 origin；
- Fallback Candidate 排名低于同等真实且满足 Intent 的候选。

## 19.6 Provider 真实性回归

所有旧有：

- Place ID；
- Country；
- Source；
- Fact；
- Constraint Gate；
- Transport Scope；

测试必须继续通过。

CI 不得调用真实付费 Provider，应使用录制 Fixture，延续仓库现有“不在 CI 花 Provider 费用”的原则。

---

# 20. 本批次验收标准

1. Worker 不再使用通用 Assignment 文本作为主要检索查询。
2. 每个 Research Query 有明确 Intent、Domain、Destination 和来源类型。
3. Generic Query 只作为 Fallback。
4. 排除类别在 Provider 调用前生效。
5. Candidate Discovery Lineage 由服务端生成。
6. 相同 Provider Identity 不重复生成 Candidate。
7. Admission 仍保持严格事实合同。
8. Ranking 不影响事实真实性判断。
9. Selection 只从 admitted candidates 中选择。
10. 通过 Admission 不再意味着必须进入 Composer。
11. Selection Plan 能说明每个候选覆盖哪些 Intent。
12. 高优先级 Intent 没有候选时触发定向补研。
13. 研究耗尽后明确保留 uncovered intent。
14. 同一输入、同一事实快照、同一政策得到相同 Selection Plan。
15. 修改主题后 Selection Plan 发生可解释变化。

---

# 21. 本批次停止条件

以下任一情况存在，禁止开始文档三：

- `museum in`、`restaurant in`、`hotel in` 仍是无条件首选查询；
- RAG 仍主要读取通用 `task_desc`；
- Candidate 没有 Query/Intent Lineage；
- 模型可以自己写正式 `intent_id`；
- Ranking 与 Admission 仍在一个函数中混合；
- Composer 仍直接读取全部 admitted candidates；
- Generic Fallback 与 Intent Candidate 没有区别；
- 修改用户主题后 Selection Plan 仍完全相同且没有合理解释；
- Candidate Gate 只知道“缺少 Visit”，不知道“缺少哪条用户要求对应的 Visit”。
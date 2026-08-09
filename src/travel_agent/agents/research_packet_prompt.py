"""Shared v2 Research Packet prompt contract for the three research workers."""

from __future__ import annotations

import json
from typing import Sequence

from ..entities.delivery_bundle import ResearchPacket
from .destination_researcher.prompts import KNOWLEDGE_BASE_NOMINATION
from .research_packet_output import ResearchWorkerKind


# ``{candidate_limit}`` is filled from the same number the response schema
# enforces on this call.  The prose, the constant and the schema must all agree
# on one number: the sentence the model actually reads is the prose, so it is
# the one that has to match the schema.
_SCOPES = {
    "destination_researcher": "具体 VisitCandidate 与具体门店 DiningCandidate；禁止类别词、小吃街或泛化餐饮建议冒充实体。",
    "accommodation_researcher": "具体 property LodgingCandidate；按真实入住区间、人数、房型、总价口径研究，合格候选动态 1 至 {candidate_limit} 个，不为凑数降质。",
    "transport_researcher": "具体 TransportCandidate；分别表达 long_distance、public_transit、flexible，多段换乘必须保留每个 segment。",
}

_ALLOWED_CANDIDATES = {
    "destination_researcher": "VisitCandidate、DiningCandidate",
    "accommodation_researcher": "LodgingCandidate",
    "transport_researcher": "TransportCandidate",
}

# 知识库 chunk 只注入 destination_researcher 一家（另一个注入点是 fast_answer，不走
# 这份合同），所以这条按 worker 分档而不是写进公用的 hard_contract —— 给交通与住宿
# 印一条「去知识库里挖地名」的规则，是让它们去够一个本轮根本不存在的抽屉。
#
# 正文的定义处只有一个：``destination_researcher.prompts.KNOWLEDGE_BASE_NOMINATION``。
# 任务提示词把同一段印在 chunk 正文旁边，这里把它作为硬合同的一条重复一次——重复的是
# **同一个常量**，不是同一段话的第二次手抄，所以两处不可能说出不一样的规则。
_KNOWLEDGE_BASE_NOMINATION = {
    "destination_researcher": KNOWLEDGE_BASE_NOMINATION,
}


def build_research_packet_system_prompt(
    *,
    worker_kind: ResearchWorkerKind,
    run_id: str,
    task_id: str,
    constraint_pack_revision: int,
    fact_data_revision: int,
    current_time: str,
    research_brief_context: str,
    candidate_limit: int,
    upstream_packet_context: str = "[]",
    active_constraint_ids: Sequence[str] = (),
) -> str:
    """``candidate_limit`` is required on purpose: it is the schema's number.

    A default here would let a caller that forgot it print a different ceiling
    from the one its own response schema enforces on the same call.
    """
    schema = json.dumps(ResearchPacket.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    nomination = _KNOWLEDGE_BASE_NOMINATION.get(worker_kind)
    nomination_clause = f"\n- {nomination}" if nomination else ""
    return f"""<role>
你是 JourneyPilot 的 {worker_kind}。你只生产强类型 Research Packet，不写报告、行程或通用 Agent Envelope。
</role>

<domain>{_SCOPES[worker_kind].format(candidate_limit=candidate_limit)}</domain>

<hard_contract>
- 只输出一个可被 json.loads 直接解析的 JSON 对象；禁止 Markdown、代码围栏、前后说明和旧 schema_version/agent/data/claims/evidence 信封。
- run_id 必须是 {run_id}；task_id 必须是 {task_id}；worker_kind 必须是 {worker_kind}；constraint_pack_revision={constraint_pack_revision}；fact_data_revision={fact_data_revision}。
- 每个现实字段必须由 FactAssertion 支持；verified FactAssertion 必须链接 external_web、external_tool 或 rag_chunk SourceRecord。禁止 derived/self-generated source。
- 用户输入、行程日期、人数、偏好、模型输出和确定性计算都不是外部 SourceRecord；禁止伪造 User Context/JourneyPilot/Assistant Provider。受控输入可直接填写 typed candidate 字段，但不要把它们伪装成外部事实。
- external_tool SourceRecord 的 identity、snapshot、content_hash 与 cache_provenance 只由系统根据当前 Tool Gateway transcript 编译；你只选择 transcript 中的 typed candidate/place_id/route_id，不复制、修补或伪造这些不可变字段。rag_chunk SourceRecord 同样由系统编译：参考知识库里每段都印有「引用标识」，你只在 FactSourceLink.source_record_id 里写那个标识，不要自己写 rag_chunk 的 source_records 条目——系统会用它注入给你的那段原文补齐整条记录；引用没印给你的标识，该链接与依赖它的事实都会被丢弃。external_web 的 snapshot 只保留本次外部结果中你要断言的关键字段：实体名称、place_id、address、provider_place_type、provider_country_code、价格、营业/无障碍等事实值必须逐字保留；禁止改写成自然语言摘要或回灌无关原始响应。每个候选最多 2 个 SourceRecord。任何模型输出的 content_hash 都不具有权威性，系统会在 typed packet admission 前按 snapshot 规范 JSON 重新计算；禁止生成 cache_provenance。
- free_web_search、tavily_search、brave_web_search、firecrawl_search 这类网页检索工具的成功结果没有 external_tool 登记路径，其 SourceRecord 一律标 source_kind="external_web"。只有 Tool Gateway 登记的地点/路线 Provider 结果和系统编译的工具失败记录才是 external_tool。
- Candidate 的 field_paths、fact_assertion_ids、source_record_ids、FieldProvenance 必须形成完整闭包；不能把缺失事实写成确定值。
- VisitCandidate 的 name、place_id、provider_place_type、provider_country_code、address 与 DiningCandidate 的 branch_name、place_id、provider_place_type、provider_country_code、address 必须各有同 entity_id 的外部 FactAssertion，事实值必须与 typed candidate 完全一致；Provider type/country 必须逐字保留地点 Provider 原始值，不得由模型归一化、推断或猜测。国家码必须与候选 destination_id 的受控目的地一致；场馆、博物馆、市场或街区的类别不能改写成餐饮门店身份。
- LodgingCandidate 的 property_name、place_id、provider_place_type、provider_country_code 与完整街道地址 address 必须各有同 entity_id 的外部 FactAssertion，事实值必须与 typed candidate 完全一致；Provider type/country 必须逐字保留地点 Provider 原始值并匹配受控目的地，只有酒店标题、区域或链接而没有稳定身份和地址的搜索结果不能支持住宿身份准入。
- Candidate.research_packet_id、fact_assertion_ids、field_paths、source_record_ids 是由父 Packet 和 entity_id 对应 FactAssertion 确定性派生的索引；不要把它们当作第二份人工维护的 evidence 清单。每条现实字段仍必须有同 entity_id 的 FactAssertion、FieldProvenance 和外部 supports 链。
- Candidate.freshness_status 必须由其引用事实状态决定：全部 verified 才是 current；含 refreshing 才是 refreshing；其余状态必须是 stale。
- 本轮刚检索且直接支持稳定实体身份的外部页面可标 verified；缺少 published_at/observed_at 本身不等于 stale。只有来源明确过期、冲突或不再适用时才标 stale。价格/实时余房未查到时使用 null/needs_confirmation 并省略对应事实，不得把实体名称、地址和受控日期一并降为 stale。
- 每条 active hard constraint 都必须有 CandidateConstraintEvaluation。缺事实时写 unknown，不得把未发现违规写成 passed；failed/unknown candidate 仍可留在内部 packet，后续 Gate 会过滤。
- 权威 active hard constraint IDs 恰为 {json.dumps(list(active_constraint_ids), ensure_ascii=False)}；每个候选的 active_constraint_ids 与 constraint_evaluations.constraint_id 必须分别精确覆盖该集合。集合为空时两个数组都必须为空，禁止模型自产约束。
- candidates 只能包含 {_ALLOWED_CANDIDATES[worker_kind]}；上游 packet 只提供上下文，禁止复制其它 Worker 的候选、事实或来源。
- 本 Packet 最多输出 {candidate_limit} 个有直接外部支持的高质量候选；只保留这些候选的最小 facts/sources/provenance 闭包，snapshot 只保留要断言的字段值（见上一条），保持紧凑输出，禁止逐字回灌原始工具响应。整份 Packet 输出务必精简，避免超长导致被截断。
- TransportCandidate.from_endpoint 必须与首个 segment.from_endpoint 完全一致，to_endpoint 必须与末个 segment.to_endpoint 完全一致；多段换乘按真实顺序完整保留。
- 必须填写 WeatherSensitivity；天气影响由确定性引擎计算，不要自行生成 WeatherImpact id。
- 酒店/餐馆只输出真实具体实体；关键价格、营业、无障碍、饮食等事实不足时如实保留 unknown，不用低质量候选凑满 {candidate_limit} 个。
- 描述性字段（VisitCandidate 的 highlights/opening_window/reservation_required，DiningCandidate 的 cuisine_types/recommended_dishes/opening_window/reservation_required，LodgingCandidate 的 room_type/facilities）不是身份字段，地点 Provider 不返回它们，所以它们靠你来填，而且**尽量都填满**：旅行者看到的卡片就靠这几行才不至于只剩一个名字和一个地址。优先用本轮真检索到的 external_web 页面或提示词里印过「引用标识」的 rag_chunk 里的内容来写，能配来源就为该 (candidate_id, field_path) 配一条同 entity_id 的 FactAssertion；确实没有检索依据时，按该类型场所的常识给出合理值即可，此时**不要**为它编造 FactAssertion 或 SourceRecord，也不要把它写进 field_paths——留空的字段才是白花的 token。{nomination_clause}
</hard_contract>

<context>
当前时间：{current_time}
{research_brief_context}
上游 Research Packets：{upstream_packet_context}
</context>

<json_schema>{schema}</json_schema>"""

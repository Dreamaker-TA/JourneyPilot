"""Normalize user constraints once before orchestration begins."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig

from ...entities.state import TravelAgentState
from ...memory.context_builder import ContextBudget, build_context_report
from ...memory.memory_store import MemoryStore
from ...memory.user_profile import UserProfileMemory
from ...models.router import get_model_router
from ...panels.constraint import ConstraintSourceLoader, referenced_context_sections

logger = logging.getLogger(__name__)

_NODE_NAME = "constraint_normalizer"

# 检索记忆事实时兜底的那句话：只在本轮连目的地和风格都取不出来时才用。
# 它不能是唯一的检索词 —— 见 ``_memory_fact_query``。
_GENERIC_CONSTRAINT_QUERY = "出行约束 偏好 预算 过敏 行动 儿童 交通 住宿 节奏"


def _memory_fact_query(state: TravelAgentState) -> str:
    """本轮真实行程意图 + 约束类词，作为记忆事实的检索词。

    **这里必须随本轮变化。** 此前它是一句写死的关键词串，于是「去杭州带老人三天」和
    「去东京拍照四天」拿**逐字相同**的一句话去检索同一个用户的记忆库 —— 三因子语义
    检索里的相关性那一因子因此对本轮意图完全失聪，``top_k=8`` 每次都从同一个方向
    截取，而记忆库里那条「上次在京都住的町屋很喜欢」永远排不进来。

    本节点跑在 ``brief_generator`` 之后，``controlled_trip_identity`` 与
    ``research_brief`` 都已可用，所以「用本轮意图当检索词」没有前置阻碍 ——
    它只是从来没有人接上。

    约束类词保留在尾部：目的地和风格决定「跟这趟有关的记忆」，而
    「过敏 / 预算 / 老人」这类词决定「哪一类记忆算约束」，两者都要在查询里。
    """

    terms: List[str] = []
    identity = state.controlled_trip_identity or {}
    if isinstance(identity, dict):
        for destination in identity.get("destinations") or []:
            if isinstance(destination, dict):
                name = str(destination.get("name") or "").strip()
                if name:
                    terms.append(name)
        origin = identity.get("origin")
        if isinstance(origin, dict):
            origin_name = str(origin.get("name") or "").strip()
            if origin_name:
                terms.append(origin_name)
        style = identity.get("style")
        if isinstance(style, dict):
            primary = str(style.get("primary") or "").strip()
            if primary:
                terms.append(primary)
            terms.extend(
                str(interest).strip()
                for interest in style.get("secondary_interests") or []
                if str(interest).strip()
            )
    try:
        brief = json.loads(state.research_brief) if state.research_brief else {}
    except (TypeError, ValueError):
        brief = {}
    if isinstance(brief, dict):
        travel_party = str(brief.get("travel_party") or "").strip()
        if travel_party:
            terms.append(travel_party)
        terms.extend(
            str(item).strip() for item in brief.get("constraints") or [] if str(item).strip()
        )

    unique_terms = list(dict.fromkeys(term for term in terms if term))
    if not unique_terms:
        return _GENERIC_CONSTRAINT_QUERY
    return " ".join(unique_terms) + " " + _GENERIC_CONSTRAINT_QUERY


async def build_run_constraint_pack(state: TravelAgentState) -> Dict[str, Any]:
    """本轮 Constraint Pack 的**唯一装配处**，深度与快问快答两条路径共用。

    偏好、画像、手写记忆、检索记忆、preset 的节奏与预算、本轮提问里抽出来的约束 ——
    这些东西在一次运行里各有几条、谁压过谁，只在这里回答一次。两条路径各装一份的
    后果会兑现成同一个用户两种答案：快路径没有 pack 时，preset 挑的节奏与预算一个字
    都到不了模型，那两项在快路径上只有 ``UserProfile`` 那条平行通道说话 —— 同一个用户
    问一句话和做一份规划拿到的是两个不同的赢家，而两边都不报错。

    记忆层在这里读**两次**，两次读的是两种东西，不能合并成一次：
    ``list_manual_facts`` 取的是用户手写的长期规则（全量、不看相关性），
    ``search_facts`` 取的是系统自动抽取出来的记忆（按本轮意图做三因子检索）。
    此前只有后者，于是「个性记忆」那一屏写下的每一条都要先被语义检索命中、再被
    Fast 模型判成「是约束」才可能进 prompt —— 界面承诺的是「每一条」，实际到达的
    是那两道过滤之后剩下的。
    """
    user_profile = None
    manual_memory_facts = None
    manual_memory_truncated = False
    memory_facts = None
    missing_layers: List[str] = []
    partial_reasons: List[str] = []

    user_id = state.user_id or "anonymous"
    try:
        user_profile = await UserProfileMemory().get_user_profile(user_id)
        if user_profile is None:
            missing_layers.extend(["manual_profile", "auto_portrait"])
    except Exception as exc:
        logger.warning("ConstraintNormalizer: user profile unavailable: %s", exc)
        missing_layers.extend(["manual_profile", "auto_portrait"])
        partial_reasons.append("user_profile_load_failed")

    # 手写记忆全量取回，不经语义检索：用户在「个性记忆」里写下的是长期规则，
    # 「这一趟跟它像不像」不该决定它在不在场。条数上限只有一处
    # （``ContextBudget.manual_memory_facts_limit``），快路径读的也是它。
    #
    # **多取一条是探针，不是第二个上限。** 排序是 ``created_at DESC``，被上限丢掉
    # 的是**最早写下的**那几条长期规则；不多取这一条，「用户正好写满了上限」与
    # 「用户写了更多、多出来的从没进过 prompt」在 pack 里长得一模一样，而截断
    # 必须出声。探针只证明「后面还有」、不证明还有几条，所以往下传的是一个是非
    # （出声那一句由 pack 层印，见 ``panels/constraint.py::_manual_memory_omitted_note``）。
    try:
        manual_memory_cap = ContextBudget().manual_memory_facts_limit
        probed = await MemoryStore().list_manual_facts(
            user_id,
            limit=manual_memory_cap + 1,
        )
        manual_memory_truncated = len(probed) > manual_memory_cap
        manual_memory_facts = probed[:manual_memory_cap]
    except Exception as exc:
        logger.warning("ConstraintNormalizer: manual memories unavailable: %s", exc)
        missing_layers.append("manual_memory")
        partial_reasons.append("manual_memory_list_failed")

    try:
        memory_facts = await MemoryStore().search_facts(
            user_id,
            _memory_fact_query(state),
            top_k=8,
        )
    except Exception as exc:
        logger.warning("ConstraintNormalizer: memory facts unavailable: %s", exc)
        missing_layers.append("memory_fact")
        partial_reasons.append("memory_fact_search_failed")

    # 一条事实只由一个来源承载。手写记忆的 importance 是最高档，语义检索本来就
    # 很容易把它捞回来 —— 不摘掉的话同一句话会同时出现在【本轮统一约束】
    # 和【参考级背景 — 不是约束】里，而后者逐字写着「不得当成硬性要求」。
    if manual_memory_facts and memory_facts is not None:
        carried = {
            str(fact.get("fact_id"))
            for fact in manual_memory_facts
            if isinstance(fact, dict) and fact.get("fact_id") is not None
        }
        memory_facts = [
            fact
            for fact in memory_facts
            if not (isinstance(fact, dict) and str(fact.get("fact_id")) in carried)
        ]

    loader = ConstraintSourceLoader.from_loaded(
        state,
        get_model_router().get_fast(),
        user_profile=user_profile,
        manual_memory_facts=manual_memory_facts,
        manual_memory_truncated=manual_memory_truncated,
        memory_facts=memory_facts,
        missing_source_layers=list(dict.fromkeys(missing_layers)),
        partial_reasons=partial_reasons,
    )
    return await loader.build_pack()


async def constraint_normalizer_node(
    state: TravelAgentState, config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """深度路径上装配 Constraint Pack 的那个节点（装配本体见
    ``build_run_constraint_pack``，快路径调的是同一个函数）。

    这个节点同时下发上下文透镜的 ``context_report``，
    因为 pack 就是「本轮参考了什么」的答案 —— 报告在这里发，才可能与模型真正读到的
    东西逐条对上。此前它由 ``scope_clarifier`` 发，列的是 ``ContextBuilder`` 那份
    **被丢弃**的装配里的偏好：屏幕上写着「本次参考的信息」，而那几条从来没有进过
    任何 prompt。快路径那侧同一份报告由 ``fast_answer_node`` 发 —— 那条路上压缩
    发生在同一个节点里，报告要等压缩结论出来才发得准。
    """
    pack = await build_run_constraint_pack(state)

    stream_queue = (config or {}).get("configurable", {}).get("stream_queue")
    if stream_queue is not None:
        # 报告为空（这一轮既没有条目进 prompt，也没压缩过）时 ``build_context_report``
        # 返回 None —— 那一轮**不发这个事件**，前端于是没有印记。要不要发只由
        # 那个函数说了算，这里不再判一次「空不空」。
        report = build_context_report(
            referenced_sections=referenced_context_sections(pack),
            compaction={"triggered": bool(state.session_compacted_this_turn)},
        )
        if report is not None:
            await stream_queue.put(("context_report", _NODE_NAME, report))

    return {
        "constraint_pack": pack,
        "constraint_pack_revision": state.constraint_pack_revision + 1,
    }

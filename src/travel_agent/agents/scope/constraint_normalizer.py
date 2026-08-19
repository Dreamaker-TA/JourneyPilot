"""Normalize user constraints once before orchestration begins."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ...entities.state import TravelAgentState
from ...local_profile import LOCAL_USER_ID
from ...memory.context_builder import ContextBudget
from ...memory.memory_store import MemoryStore
from ...memory.user_profile import UserProfileMemory
from ...models.router import get_model_router
from ...panels.constraint import ConstraintSourceLoader

logger = logging.getLogger(__name__)

# 检索记忆事实时兜底的那句话：只在本轮连目的地和风格都取不出来时才用。
# 它不能是唯一的检索词 —— 见 ``_memory_fact_query``。
_GENERIC_CONSTRAINT_QUERY = "出行约束 偏好 预算 过敏 行动 儿童 交通 住宿 节奏"


@dataclass(frozen=True)
class LoadedConstraintSources:
    user_profile: Any
    manual_memory_facts: Optional[List[Dict[str, Any]]]
    manual_memory_truncated: bool
    memory_facts: Optional[List[Dict[str, Any]]]
    missing_layers: List[str]
    partial_reasons: List[str]


def _memory_fact_query(state: TravelAgentState) -> str:
    """本轮真实行程意图 + 约束类词，作为记忆事实的检索词。

    **这里必须随本轮变化。** 此前它是一句写死的关键词串，于是「去杭州带老人三天」和
    「去东京拍照四天」拿**逐字相同**的一句话去检索同一个用户的记忆库 —— 三因子语义
    检索里的相关性那一因子因此对本轮意图完全失聪，``top_k=8`` 每次都从同一个方向
    截取，而记忆库里那条「上次在京都住的町屋很喜欢」永远排不进来。

    这里只消费受控旅行身份。自由文本要求的解释仍只能发生在
    Request Contract 归一化边界。

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
    unique_terms = list(dict.fromkeys(term for term in terms if term))
    if not unique_terms:
        return _GENERIC_CONSTRAINT_QUERY
    return " ".join(unique_terms) + " " + _GENERIC_CONSTRAINT_QUERY


async def load_constraint_sources(state: TravelAgentState) -> LoadedConstraintSources:
    user_profile = None
    manual_memory_facts = None
    manual_memory_truncated = False
    memory_facts = None
    missing_layers: List[str] = []
    partial_reasons: List[str] = []

    user_id = state.user_id or LOCAL_USER_ID
    try:
        user_profile = await UserProfileMemory().get_user_profile(user_id)
        if user_profile is None:
            missing_layers.extend(["manual_profile", "auto_portrait"])
    except Exception as exc:
        logger.warning("ConstraintNormalizer: user profile unavailable: %s", exc)
        missing_layers.extend(["manual_profile", "auto_portrait"])
        partial_reasons.append("user_profile_load_failed")

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

    return LoadedConstraintSources(
        user_profile=user_profile,
        manual_memory_facts=manual_memory_facts,
        manual_memory_truncated=manual_memory_truncated,
        memory_facts=memory_facts,
        missing_layers=list(dict.fromkeys(missing_layers)),
        partial_reasons=partial_reasons,
    )


async def build_run_constraint_pack(
    state: TravelAgentState,
    *,
    loaded: Optional[LoadedConstraintSources] = None,
    precomputed_free_text_constraints: Optional[
        Dict[str, List[Dict[str, Any]]]
    ] = None,
) -> Dict[str, Any]:
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
    loaded = loaded or await load_constraint_sources(state)

    loader = ConstraintSourceLoader.from_loaded(
        state,
        get_model_router().get_fast(),
        user_profile=loaded.user_profile,
        manual_memory_facts=loaded.manual_memory_facts,
        manual_memory_truncated=loaded.manual_memory_truncated,
        memory_facts=loaded.memory_facts,
        missing_source_layers=loaded.missing_layers,
        partial_reasons=loaded.partial_reasons,
        precomputed_free_text_constraints=precomputed_free_text_constraints,
    )
    return await loader.build_pack()

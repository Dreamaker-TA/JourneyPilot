"""Run-scoped Personal Constraint Pack builder.

Constraint Pack v1 是个人出行约束的后端唯一事实源：
request_contract_normalizer → source loader → shared semantic extraction + deterministic fallback →
ConstraintExtractionResult → PersonalConstraintPack。Planner 与 Workers 只消费 state 中同一个 pack。

七个来源三类处理（JP-02-02 §A.1）：
  - ``manual_profile``（TravelPreference）/ ``preset``：结构字段直接映射 category，无 LLM。
  - ``manual_memory``（「记忆与偏好」页手写的那几条）：**原文**直接映成一条 item，无 LLM。
    它不进自由文本抽取队列 —— 那道队列会把判成「不是约束」的整条丢弃，而这一层的
    产品语义是「用户写下的每一条都要带着」，少一条就不成立。
  - ``auto_portrait``：单一参考级软信号块，不拆离散 item，锁 internal_only、永不升 hard。
  - ``session_anchor`` / ``current_query`` / ``memory_fact``：Fast 模型单次结构化抽取
    （仅做归类 / 总结 / params），``type`` / ``confidence`` / ``priority`` / ``visibility``
    由**来源层确定性封顶**（「用户显式声明 > 系统推理」），LLM 无权突破。

输入契约为 ``build_constraint_pack(state, fast_llm, *, user_profile=None,
manual_memory_facts=None, memory_facts=None)``；``ConstraintSourceLoader`` 在前置节点装配
结构化 profile / 手写记忆 / 检索记忆，并把上下文完整度写入 meta。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..entities.user import DIETARY_ALLERGY_OPTION
from ..utils.json_helpers import safe_parse_json

# JourneyPilot v1 核心约束类 + extension + other（LLM / fallback 归类目标）。
_CORE_CATEGORIES = {
    "food_allergy",
    "budget_cap",
    "elderly_mobility",
    "child_friendly",
    "transport_constraint",
    "accommodation_preference",
    "pace_preference",
}
_EXTENSION_CATEGORIES = {
    "destination_preference",
    "dietary_restriction",
    "health_condition",
}
_ALL_CATEGORIES = _CORE_CATEGORIES | _EXTENSION_CATEGORIES | {"other"}

# 默认 hard 的 category（其余按硬性措辞判断；budget_cap 仅「上限」措辞 + 显式来源才 hard）。
# food_allergy 故意不在此集合：调研阶段无法验证餐厅全菜单是否含过敏原，
# 产品语义为 soft「用餐提醒」（点菜避开），不得作为 candidate admission hard 门槛。
_HARD_CATEGORIES = {"elderly_mobility", "health_condition"}
_CONDITIONAL_HARD_CATEGORIES = {
    "budget_cap",
    "child_friendly",
    "transport_constraint",
    "accommodation_preference",
    "pace_preference",
}
_BUDGET_HARD_HINTS = (
    "上限",
    "不超过",
    "以内",
    "别超",
    "最多",
    "封顶",
    "no more than",
    "cap",
    "max",
)
_HARD_HINTS = _BUDGET_HARD_HINTS + (
    "必须",
    "务必",
    "不能",
    "不要",
    "别",
    "禁止",
    "不得",
    "不安排",
    "不早于",
    "不晚于",
    "要求",
    "禁烟",
    "避开",
    "避免",
    "坚持",
    "只能",
    "只坐",
    "只用",
    "avoid",
    "must",
    "never",
    "no ",
)

_LOCAL_TRANSPORT_MODE_ALIASES = {
    "walk": ("步行", "徒步", "walk", "walking"),
    "bike": ("骑行", "自行车", "单车", "bike", "bicycle", "cycling"),
    "drive": ("自驾", "驾车", "开车", "drive", "driving"),
    "taxi": ("出租车", "打车", "taxi", "cab"),
    "ride_hailing": ("网约车", "ride_hailing", "ride hailing", "rideshare"),
}
_LOCAL_MODE_LOCK_HINTS = ("必须", "坚持", "只坐", "只用", "只能", "务必", "must", "only")
_LOCAL_MODE_AVOID_HINTS = (
    "不要",
    "不想",
    "不坐",
    "不走",
    "不能",
    "禁止",
    "避免",
    "避开",
    "少坐",
    "少走",
    "avoid",
    "never",
    "no ",
)
_LOCAL_MODE_PREFER_HINTS = (
    "优先",
    "偏好",
    "喜欢",
    "尽量",
    "坚持",
    "选择",
    "希望",
    "我要",
    "prefer",
    "preferred",
    "must",
    "only",
)

# 可升 hard 的显式来源（JP-02-02 §A.1：用户显式声明 > 系统推理）。
# ``preset`` **不在**里面：挑一个旅行风格是选一档偏好，不是下一条硬性要求
# （风格里写的「预算档位：经济」和手填画像里的档位一样，都是 soft）。
# ``manual_memory`` 也**不在**里面，而且这一条是刻意的：手写记忆是一句没有经过任何
# 归类的自由文本，产品把它呈现为「偏好」，所以它进 prompt 但不产生新的硬约束 ——
# 一条「我只坐出租车」若升成 hard，就会顺着 transport_constraint 变成 required
# connector pair → gap → 定向补研，把写死的墙钟预算顶出去。
# 它落在 ``other`` 这一档（见 ``_map_manual_memory_facts``），所以 ``_default_type``
# 结构上就给不出 hard；写在这里是为了让「加不加」是一次记录在案的决定，而不是遗漏。
_EXPLICIT_SOURCES = {
    "manual_profile",
    "session_anchor",
    "current_query",
    "plan_gate_amendment",
    "run_supplement",
}

logger = logging.getLogger(__name__)

# 一条抽取出来的约束该有多长：产品里最长的合规摘要（老人无障碍那条
# 「避免长楼梯；优先电梯、无障碍路线与休息安排」）是 24 个字。超过这个数**而且**与
# 输入逐字相同的，不是抽取结果，是把输入原样退回来了（批次 4 在计划门上实测到过：
# 那一屏把用户整段请求印成了一条「本轮必须遵守」）。短输入等于输出是正常的
# —— 手写记忆「我不吃香菜」本来就该原样带着。
_MAX_EXTRACTED_CONSTRAINT_CHARS = 60

# 单值类（新旧冲突按 recency 仲裁，旧者 superseded）；多值类保留全部（§A.4）。
_SINGULAR_CATEGORIES = {"budget_cap", "pace_preference"}

# 单值类冲突的**来源优先级**（大者胜），用在 ``updated_at`` 打平之后。
#
# 这张表是必需的，不是锦上添花：一次 pack 装配里 manual_profile / preset /
# current_query / brief 拿到的是**同一个** ``now``，所以只按 ``updated_at`` 排序时，
# 谁赢完全取决于谁先被 append —— 一个实现细节。而这个仓正是在这种地方吃过亏：
# 「两处各写一份，其中一份静默胜出」。
#
# 顺序的理由：用户这一句话里刚说的 > 他为这一趟挑的风格 > 本会话早前说过的 >
# 他手写下来的长期规则 > 常设默认 > 系统从历史里推出来的。preset 高于 manual_profile
# 是因为它更**具体**：画像是「我平常这样」，preset 是「这一趟我要这样」；同理
# manual_memory 高于 manual_profile —— 亲手写下一整句话比勾一个选项更具体。
#
# **每一个能被 ``build_constraint_pack`` 写进 ``source_refs`` 的来源都必须在这张表里。**
# 漏一个拿的是 ``_resolve_conflicts`` 里 ``.get(..., -1)`` 的默认值，排在
# ``auto_portrait``（0）**之下** —— 用户手写的「预算别超两千」会输给系统推断的画像，
# 不报错、不留痕。这张表的完整性由真实打包出来的包反查守护：新增来源必须在这里登记。
_SINGULAR_SOURCE_PRECEDENCE = {
    "current_query": 7,
    "plan_gate_amendment": 6,
    "run_supplement": 5,
    "preset": 4,
    "session_anchor": 3,
    "manual_memory": 2,
    "manual_profile": 1,
    "memory_fact": 0,
    "auto_portrait": 0,
}

# 印进 prompt 的三个**互不相通**的行数预算（见 ``_budgeted_rows``）。
# 三个数各自的作用是防失控，不是排序 —— 旧的单池 16 不是量出来的容量，它只是一个能让
# 偏好悄悄消失的数。
#
# 偏好那一档的数必须**大于产品自己能产出的偏好行数**，否则界面上摆着的东西有一部分
# 从写下来那天起就到不了模型。这一档今天有两个产出方，两个都要算进去：
#
#   * 「我的偏好」那一屏。深度路径是**一个取值一条 item**（三个交通偏好就是三条，
#     见 ``_dedupe_constraints`` 的键），所以勾满六组
#     （选项表在 ``entities/user.py::TRAVEL_PREFERENCE_GROUPS`` 收成一处）
#     = 6 风格 + 6 交通 + **7 饮食**（饮食组含过敏那一项）+ 3 个单值 = **22 行**。
#   * 「个性记忆」那一屏手写的记忆。它每条一行，条数上限的**唯一定义处**是
#     ``memory/context_builder.py::ContextBudget.manual_memory_facts_limit``（今天是 20）。
#
# 取 45 = 22 + 20 + 3，多出的三行留给本轮从提问里抽出的软偏好。这个数必须随
# 上面两个产出方的实际规模同步：任何一侧变大而这里没跟上，打包出来的行数就会
# 超出预算。所以这里不需要（也不应该）在运行期去 import 预算层算这个数。
_PROMPT_HARD_LINES = 12
_PROMPT_PREFERENCE_LINES = 45
_PROMPT_REFERENCE_LINES = 6

_EXTRACT_SYSTEM_PROMPT = (
    "你是个人出行约束抽取器。从给定文本项中识别用户的真实出行约束，"
    "归类为以下类别之一：food_allergy（食物过敏）、budget_cap（预算上限）、"
    "elderly_mobility（老人行动能力）、child_friendly（儿童友好）、"
    "transport_constraint（交通禁忌/偏好）、accommodation_preference（住宿偏好）、"
    "pace_preference（节奏偏好）、destination_preference（目的地偏好）、"
    "dietary_restriction（饮食限制）、health_condition（健康状况）、other（其他）。"
    "只抽取文本中真实存在的约束，不臆造、不推断未表达的约束。"
    "为每项产出简洁的 canonical value（可直接作「已考虑 X」标签）；"
    "对 budget_cap 抽 params {amount, currency, per}，amount 只能是数值或 null；"
    "「经济」「中等」「适中」「奢华」这类档位描述词不是数值，amount 必须写 null，"
    "把档位措辞留在 value 里；"
    "对 food_allergy 抽 params {allergens: []}；"
    "对 transport_constraint 抽 params {avoid_overnight, earliest_departure_local, "
    "latest_arrival_local, preferred_local_modes, excluded_local_modes, locked_local_mode}；"
    "对 accommodation_preference 抽 params {required_facilities: []}；"
    "对 elderly_mobility/health_condition 抽 params {max_continuous_walk_minutes, "
    "avoid_long_stairs, prefer: [], unknown_facility_policy}。"
    "时间使用 HH:MM，设施和偏好使用英文 canonical token；未表达的字段不要补。"
    "只输出 JSON，不要解释。"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cid(*parts: Any) -> str:
    import hashlib

    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return f"c_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"


def _get(state: Any, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except Exception:
        return None


def _normalize_category(cat: Any) -> str:
    c = str(cat or "").strip().lower()
    return c if c in _ALL_CATEGORIES else "other"


def _default_type(category: str, value: str, source: str) -> str:
    """category → 默认 type（§A.2.1）；budget_cap 仅显式来源 + 上限措辞才 hard。"""
    if category in _HARD_CATEGORIES:
        return "hard"
    if category in _CONDITIONAL_HARD_CATEGORIES:
        v = (value or "").lower()
        hints = _BUDGET_HARD_HINTS if category == "budget_cap" else _HARD_HINTS
        if (source in _EXPLICIT_SOURCES or source == "memory_fact") and any(h in v for h in hints):
            return "hard"
    return "soft"


def _priority_for(ctype: str, confidence: str) -> str:
    if ctype == "hard":
        return "hard"
    if confidence == "high":
        return "strong"
    if confidence == "medium":
        return "normal"
    return "weak"


def _canonical_local_time(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _canonical_transport_params(
    value: str, params: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    normalized = dict(_canonical_transport_mode_params(value, params) or {})
    avoid = list(normalized.get("avoid") or [])
    if any(token in value for token in ("禁过夜", "禁止过夜", "不安排过夜", "不要过夜", "避免过夜", "不过夜交通")):
        normalized["avoid_overnight"] = True
    if normalized.get("avoid_overnight") is not True:
        normalized.pop("avoid_overnight", None)

    earliest_patterns = (
        r"(\d{1,2}:\d{2})\s*(?:前|之前)\s*(?:不|不要|不得|禁止).{0,6}(?:出发|发车)",
        r"(?:不早于|最早)\s*(\d{1,2}:\d{2}).{0,6}(?:出发|发车)?",
    )
    latest_patterns = (
        r"(\d{1,2}:\d{2})\s*(?:后|之后)\s*(?:不|不要|不得|禁止).{0,6}(?:抵达|到达)",
        r"(?:不晚于|最晚)\s*(\d{1,2}:\d{2}).{0,6}(?:抵达|到达)?",
    )
    for key, patterns in (
        ("earliest_departure_local", earliest_patterns),
        ("latest_arrival_local", latest_patterns),
    ):
        current = _canonical_local_time(normalized.get(key))
        if current is None:
            for pattern in patterns:
                match = re.search(pattern, value)
                if match:
                    current = _canonical_local_time(match.group(1))
                    if current:
                        break
        if current:
            normalized[key] = current
        else:
            normalized.pop(key, None)
    if avoid:
        normalized["avoid"] = list(dict.fromkeys(str(item) for item in avoid if str(item)))
    else:
        normalized.pop("avoid", None)
    return normalized or None


def _canonical_accommodation_params(
    value: str, params: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    normalized = dict(params or {})
    facilities = list(normalized.pop("required_facilities", []) or [])
    facilities.extend(normalized.pop("needs", []) or [])
    if any(token in value for token in ("禁烟", "无烟")):
        facilities.append("non_smoking")
    if any(token in value for token in ("独立卫浴", "独立卫生间", "独卫")):
        facilities.append("private_bath")
    if any(token in value for token in ("必须有电梯", "要有电梯", "需要电梯", "酒店有电梯", "住宿有电梯")):
        facilities.append("elevator")
    facility_aliases = {
        "non-smoking": "non_smoking",
        "nonsmoking": "non_smoking",
        "无烟": "non_smoking",
        "禁烟": "non_smoking",
        "private_bathroom": "private_bath",
        "private-bathroom": "private_bath",
        "独立卫浴": "private_bath",
        "独立卫生间": "private_bath",
        "独卫": "private_bath",
        "电梯": "elevator",
    }
    facilities = list(
        dict.fromkeys(
            facility_aliases.get(str(item).strip().lower(), str(item).strip())
            for item in facilities
            if str(item).strip()
        )
    )
    if facilities:
        normalized["required_facilities"] = facilities
        normalized["unknown_facility_policy"] = "needs_confirmation"
    avoid = list(dict.fromkeys(str(item) for item in normalized.get("avoid") or [] if str(item)))
    if avoid:
        normalized["avoid"] = avoid
    else:
        normalized.pop("avoid", None)
    return normalized or None


def _canonical_mobility_params(
    value: str, params: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    normalized = dict(params or {})
    walk = _positive_int_param(normalized.pop("max_continuous_walk_minutes", None))
    if walk is None:
        match = re.search(r"(?:连续)?步行.{0,6}(?:不超过|最多|超过)\s*(\d{1,3})\s*分钟", value)
        if match:
            walk = _positive_int_param(int(match.group(1)))
    if walk is not None:
        normalized["max_continuous_walk_minutes"] = walk
    if any(token in value for token in ("少楼梯", "少爬楼梯", "避免楼梯", "避免爬楼", "不要爬楼", "长楼梯", "长距离爬楼梯")):
        normalized["avoid_long_stairs"] = True
    prefer = list(normalized.get("prefer") or [])
    if any(token in value for token in ("电梯", "无障碍", "休息", "慢节奏", "膝", "腿脚", "行动不便")):
        prefer.extend(["elevator", "accessible_route", "rest_break"])
    normalized.pop("needs", None)
    normalized["prefer"] = list(dict.fromkeys(str(item) for item in prefer if str(item)))
    normalized["unknown_facility_policy"] = "needs_confirmation"
    return normalized


def _positive_int_param(value: Any) -> Optional[int]:
    """Admit a canonical positive integer without coercion or defaults."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


# A budget amount is one number, optionally carrying a currency mark or
# thousands separators ("8000"、"8,000 元"、"¥800").
_BUDGET_AMOUNT_PATTERN = re.compile(
    r"[¥￥]?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:元|块|人民币|rmb|cny)?",
    re.IGNORECASE,
)


def _budget_amount_param(value: Any) -> Optional[float]:
    """Admit a numeric budget cap; extracted wording is never coerced into one.

    ``amount`` is a numeric slot fed by model extraction, so it can arrive as a
    level word ("中等"、"经济") instead of a number.  A cap exists only when the
    value is a positive number, or a string that is entirely one such number;
    anything else means the run carries no numeric cap and the wording stays in
    the constraint value text.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
    elif isinstance(value, str):
        match = _BUDGET_AMOUNT_PATTERN.fullmatch(value.strip())
        if match is None:
            return None
        amount = float(match.group(1).replace(",", ""))
    else:
        return None
    return amount if amount > 0 else None


def _canonical_constraint_params(
    category: str, value: str, params: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    if category == "transport_constraint":
        return _canonical_transport_params(value, params)
    if category == "accommodation_preference":
        return _canonical_accommodation_params(value, params)
    if category in {"elderly_mobility", "health_condition"}:
        return _canonical_mobility_params(value, params)
    if category == "food_allergy":
        return _canonical_allergy_params(value, params)
    if category == "budget_cap":
        normalized = dict(params or {})
        amount = _budget_amount_param(normalized.get("amount"))
        if amount is not None:
            normalized["amount"] = amount
        else:
            normalized.pop("amount", None)
        normalized["currency"] = str(normalized.get("currency") or "CNY").upper()
        normalized["per"] = str(normalized.get("per") or "total")
        return normalized
    return dict(params) if params else None


def _enforcement_policy(category: str, params: Optional[Dict[str, Any]], ctype: str) -> Dict[str, Any]:
    if ctype != "hard":
        return {"enforcement_scope": ["advisory"], "candidate_worker_kinds": []}
    if category == "budget_cap":
        return {"enforcement_scope": ["composition"], "candidate_worker_kinds": []}
    if category == "transport_constraint":
        candidate_local = bool(
            params
            and any(
                key in params
                for key in ("avoid_overnight", "earliest_departure_local", "latest_arrival_local", "avoid")
            )
        )
        return {
            "enforcement_scope": ["candidate", "composition"] if candidate_local else ["composition"],
            "candidate_worker_kinds": ["transport_researcher"] if candidate_local else [],
        }
    if category == "accommodation_preference":
        return {
            "enforcement_scope": ["candidate"],
            "candidate_worker_kinds": ["accommodation_researcher"],
        }
    if category in {"elderly_mobility", "health_condition", "pace_preference"}:
        return {"enforcement_scope": ["composition"], "candidate_worker_kinds": []}
    if category in {"child_friendly", "dietary_restriction", "destination_preference"}:
        return {
            "enforcement_scope": ["candidate"],
            "candidate_worker_kinds": ["destination_researcher"],
        }
    return {"enforcement_scope": [], "candidate_worker_kinds": [], "unsupported_hard_category": True}


def _public_summary(category: str, value: str, params: Optional[Dict[str, Any]]) -> str:
    params = params or {}
    if category == "budget_cap":
        # Only a numeric cap gets a numeric summary; budget wording without a
        # number is summarized by its own text.
        amount = _budget_amount_param(params.get("amount"))
        if amount is not None:
            label = "总预算" if params.get("per") == "total" else "每晚预算" if params.get("per") == "night" else "每日预算"
            return f"{label}不超过 {amount:.0f} {params.get('currency', 'CNY')}"
    if category == "transport_constraint":
        parts = []
        if params.get("avoid_overnight"):
            parts.append("不安排过夜交通")
        if params.get("earliest_departure_local"):
            parts.append(f"不早于 {params['earliest_departure_local']} 出发")
        if params.get("latest_arrival_local"):
            parts.append(f"不晚于 {params['latest_arrival_local']} 抵达")
        return "；".join(parts) or value
    if category == "accommodation_preference":
        labels = {"non_smoking": "禁烟", "private_bath": "独立卫浴", "elevator": "电梯"}
        facilities = [labels.get(str(item), str(item)) for item in params.get("required_facilities") or []]
        return "住宿必须提供" + "、".join(facilities) if facilities else value
    if category in {"elderly_mobility", "health_condition"}:
        parts = []
        if params.get("max_continuous_walk_minutes"):
            parts.append(f"连续步行不超过 {params['max_continuous_walk_minutes']} 分钟")
        if params.get("avoid_long_stairs"):
            parts.append("避免长楼梯")
        parts.append("优先电梯、无障碍路线与休息安排")
        return "；".join(dict.fromkeys(parts))
    return value


def _make_item(
    *,
    category: str,
    value: str,
    params: Optional[Dict[str, Any]],
    source: str,
    confidence: str,
    visibility: str,
    updated_at: str,
    origin_ref: Any,
) -> Dict[str, Any]:
    """装配 ConstraintItem，并执行来源层 hard 封顶（JP-02-02 §A.1 / §A.2.2）。"""
    params = _canonical_constraint_params(category, value, params)
    ctype = _default_type(category, value, source)
    # 封顶：非显式来源 / 非 high 档 memory_fact 不得升 hard。
    if ctype == "hard" and source not in _EXPLICIT_SOURCES and not (source == "memory_fact" and confidence == "high"):
        ctype = "soft"
    policy = _enforcement_policy(category, params, ctype)
    return {
        "constraint_id": _cid(category, value, params, source, origin_ref),
        "category": category,
        "value": value,
        "params": params or None,
        "type": ctype,
        "priority": _priority_for(ctype, confidence),
        "source_refs": [{
            "source": source,
            "origin_ref": origin_ref,
            "confidence": confidence,
            "updated_at": updated_at,
        }],
        "confidence": confidence,
        "updated_at": updated_at,
        "recency": 0.0,  # v0 占位；仲裁以 updated_at 为准（§A.4）
        "public_summary": _public_summary(category, value, params),
        "visibility": visibility,
        "status": "active",
        "supersedes": None,
        **policy,
    }


def _contains_mode_alias(text: str, alias: str) -> bool:
    if alias.isascii() and alias.replace("_", "").replace(" ", "").isalpha():
        return bool(re.search(rf"(?<![a-z]){re.escape(alias.casefold())}(?![a-z])", text.casefold()))
    return alias in text


def _mode_text_intent(value: str, aliases: tuple[str, ...]) -> Optional[str]:
    """Return the nearest explicit intent governing a mentioned local mode.

    Chinese negative lists commonly put the negation only before the first item,
    for example ``不要自行车、步行、出租车或网约车``.  A fixed eight-character
    window therefore misclassifies the later items.  Read the nearest intent in
    the same sentence instead, while allowing a later positive phrase (for
    example ``不要步行，优先打车``) to start a new intent scope.
    """
    normalized = value.casefold()
    strong_boundary = re.compile(r"[。；;.!?！？\n]")
    intent_groups = (
        ("excluded", _LOCAL_MODE_AVOID_HINTS),
        ("locked", _LOCAL_MODE_LOCK_HINTS),
        ("preferred", _LOCAL_MODE_PREFER_HINTS),
    )
    decisions: list[tuple[int, int, str]] = []
    for alias in aliases:
        alias_value = alias.casefold()
        for alias_match in re.finditer(re.escape(alias_value), normalized):
            prefix = normalized[: alias_match.start()]
            boundaries = list(strong_boundary.finditer(prefix))
            scope_start = boundaries[-1].end() if boundaries else 0
            scoped_prefix = normalized[scope_start : alias_match.start()]
            for decision_priority, (decision, hints) in enumerate(intent_groups):
                for hint in hints:
                    hint_index = scoped_prefix.rfind(hint)
                    if hint_index >= 0:
                        distance = len(scoped_prefix) - hint_index - len(hint)
                        decisions.append((distance, decision_priority, decision))
    if not decisions:
        return None
    return min(decisions)[2]


def _canonical_transport_mode_params(
    value: str,
    params: Optional[Dict[str, Any]],
    *,
    require_intent_hint: bool = False,
) -> Optional[Dict[str, Any]]:
    """Bind explicit local-mode intent to typed params without inventing a route."""
    normalized = dict(params or {})
    raw_preferred = normalized.get("preferred_local_modes")
    preferred = list(raw_preferred) if isinstance(raw_preferred, list) else []
    raw_excluded = normalized.get("excluded_local_modes")
    excluded = list(raw_excluded) if isinstance(raw_excluded, list) else []
    locked = str(normalized.get("locked_local_mode") or "").strip() or None
    for mode, aliases in _LOCAL_TRANSPORT_MODE_ALIASES.items():
        matching_aliases = [alias for alias in aliases if _contains_mode_alias(value, alias)]
        if not matching_aliases:
            continue
        text_intent = _mode_text_intent(value, tuple(matching_aliases))
        if text_intent == "excluded":
            preferred = [candidate for candidate in preferred if candidate != mode]
            excluded.append(mode)
            if locked == mode:
                locked = None
            continue
        if require_intent_hint and text_intent is None:
            continue
        if text_intent in {"preferred", "locked"}:
            excluded = [candidate for candidate in excluded if candidate != mode]
        preferred.append(mode)
        if text_intent == "locked":
            locked = mode
    preferred = [
        mode
        for mode in dict.fromkeys(preferred)
        if mode in _LOCAL_TRANSPORT_MODE_ALIASES and mode not in set(excluded)
    ]
    excluded = [
        mode for mode in dict.fromkeys(excluded) if mode in _LOCAL_TRANSPORT_MODE_ALIASES
    ]
    if preferred:
        normalized["preferred_local_modes"] = preferred
    else:
        normalized.pop("preferred_local_modes", None)
    if excluded:
        normalized["excluded_local_modes"] = excluded
    else:
        normalized.pop("excluded_local_modes", None)
    if locked in preferred:
        normalized["locked_local_mode"] = locked
    else:
        normalized.pop("locked_local_mode", None)
    return normalized or None


def _map_manual_profile(user_profile: Any, updated_at: str) -> List[Dict[str, Any]]:
    """manual_profile：TravelPreference 结构字段直接映射 category（§A.2.1，无 LLM）。

    **这里映的是 ``TravelPreference`` 的全部六组取值字段，一组不多一组不少，而且这是
    偏好抵达模型的唯一一处。** 快路径此前另有一条平行通道
    （``entities/user.py::UserProfile.to_context_str()`` 印成散文），两侧还各写了一份
    互不包含的名单：这里多映一个 ``preferred_destinations``（产品里根本没有写入方），
    少映 ``travel_styles`` —— 界面上第一组、用户最常勾的那一组偏好，在深度规划的
     prompt 里一个字都没有。名单后来收成一份、通道也收成一条：那条散文没有了，
    快慢两路读的都是这里映出来的 item，于是「preset 压过常设画像」这条单值类仲裁
    在两条路上是同一个结果。名单只有这一处，两条路径的赢家必然一致。
    """
    prefs = getattr(user_profile, "preferences", None)
    if prefs is None:
        return []
    items: List[Dict[str, Any]] = []

    # travel_styles 落 ``other``：v1 的类别表里没有「旅行风格」这一档，而它确实是一条
    # 用户显式声明的软偏好。``other`` 永远给 soft（``_default_type`` 两个 hard 集合都
    # 不含它），所以 ``_enforcement_policy`` 在 ``ctype != "hard"`` 那一支就返回了
    # advisory，打不上 ``unsupported_hard_category`` 标记 —— plan_gate 那条
    # 「硬约束合同不完整」的 RuntimeError 踩不响。
    # 取值带上「旅行风格：」这个标签，理由与 budget_cap 同：光一个「自然风光」进了
    # prompt 与上下文透镜，读的人（和模型）无从知道它在说哪一档偏好。
    for style in getattr(prefs, "travel_styles", None) or []:
        items.append(
            _make_item(
                category="other",
                value=f"旅行风格：{style}",
                params=None,
                source="manual_profile",
                confidence="high",
                visibility="user_visible",
                updated_at=updated_at,
                origin_ref="profile.travel_styles",
            )
        )

    # 饮食那一组里只有一个选项是过敏（``DIETARY_ALLERGY_OPTION``），它落 ``food_allergy``，
    # 其余落 ``dietary_restriction``。**判的是「是不是那一项」，不是「文本里有没有『过敏』
    # 这两个字」**：取值是闭合词表，所以「表里哪一项是过敏」这件事有确定答案，
    # 而按子串判会在下一个含「过敏」的选项上悄悄多认一个（那种选项本身已被
    # ``entities/user.py::_the_one_allergy_option`` 挡住）。
    #
    # **这一项产出的是「有过敏，过敏原未知」，不是「过敏原叫『有食物过敏』」。**
    # 改前这里写的是 ``params={"allergens": [str(d)]}`` —— 把选项文案当成过敏原名塞进
    # 清单，于是下游的用餐提醒会印出「请点餐时避开含<选项文案>的菜品」，一句系统根本
    # 不知道的话。过敏原只有一个产地：对话/记忆那条 LLM 抽取路径（``params.allergens``）。
    # 两条路因此在 pack 里是两条能分辨的 item，分辨依据是
    # ``params.allergen_detail == "unknown"``（见 ``_allergy_declares_unknown_detail``）。
    for d in getattr(prefs, "dietary_restrictions", None) or []:
        if str(d) == DIETARY_ALLERGY_OPTION:
            items.append(
                _make_item(
                    category="food_allergy",
                    value=f"{DIETARY_ALLERGY_OPTION}（具体过敏原未知）",
                    params={"allergen_detail": _ALLERGEN_DETAIL_UNKNOWN},
                    source="manual_profile",
                    confidence="high",
                    visibility="user_visible",
                    updated_at=updated_at,
                    origin_ref="profile.dietary_restrictions",
                )
            )
            continue
        items.append(
            _make_item(
                category="dietary_restriction",
                value=str(d),
                params=None,
                source="manual_profile",
                confidence="high",
                visibility="user_visible",
                updated_at=updated_at,
                origin_ref="profile.dietary_restrictions",
            )
        )

    blvl = str(getattr(prefs, "typical_budget_level", "") or "").strip()
    if blvl:
        items.append(
            _make_item(
                category="budget_cap",
                value=f"预算档位：{blvl}",  # 档位非硬上限 → soft（§A.2.1）
                params=None,
                source="manual_profile",
                confidence="high",
                visibility="user_visible",
                updated_at=updated_at,
                origin_ref="profile.typical_budget_level",
            )
        )

    acc = str(getattr(prefs, "accommodation_type", "") or "").strip()
    if acc:
        items.append(
            _make_item(
                category="accommodation_preference", value=acc, params=None,
                source="manual_profile", confidence="high", visibility="user_visible",
                updated_at=updated_at, origin_ref="profile.accommodation_type",
            )
        )

    pace = str(getattr(prefs, "pace", "") or "").strip()
    if pace:
        items.append(
            _make_item(
                category="pace_preference", value=pace, params=None,
                source="manual_profile", confidence="high", visibility="user_visible",
                updated_at=updated_at, origin_ref="profile.pace",
            )
        )

    for t in getattr(prefs, "preferred_transport", None) or []:
        items.append(
            _make_item(
                category="transport_constraint", value=str(t), params=None,
                source="manual_profile", confidence="high", visibility="user_visible",
                updated_at=updated_at, origin_ref="profile.preferred_transport",
            )
        )

    return items


def _map_manual_memory_facts(
    manual_memory_facts: Any, updated_at: str
) -> List[Dict[str, Any]]:
    """manual_memory：用户在「个性记忆」里手写的每一条，**原文**直接映成一条 item。

    形状照抄 ``_map_manual_profile``（结构直映、无 LLM），理由是这一层的产品语义：
    界面收的是一整句用户自己写的话，它没有类别、没有 params，也不需要有 —— 只需要
    原样出现在模型读到的那一段里。

    **它刻意不进自由文本抽取队列。** 走那道队列的话，Fast 模型逐条判
    ``is_constraint``，判 false 的整条被丢弃（见 ``_llm_extract``），于是「用户写了五条、
    模型读到三条」，而丢掉哪两条取决于一次模型调用 —— 界面上那句承诺当场不成立，
    且不留任何痕迹。

    ``category`` 恒为 ``other``，这不是偷懒：手写记忆是一句**没有被归类过**的话，
    ``other`` 就是诚实的答案。它同时是结构性的封顶 —— ``_HARD_CATEGORIES`` 与
    ``_CONDITIONAL_HARD_CATEGORIES`` 都不含 ``other``，所以 ``_default_type`` 无论措辞
    多硬（「必须只坐出租车」）都只会给 ``soft``，也就不会顺着 transport_constraint 变成
    required connector pair。``visibility`` 是 ``user_visible``：这是用户亲口写下的，
    该进【本轮统一约束】那一段，不是【参考级背景 — 不是约束】。

    ``updated_at`` 用这条记忆自己的 ``created_at``，取不到才退到本轮的 ``now`` ——
    单值类仲裁按时间排序，把一条三个月前写下的规则记成「刚刚说的」会让它凭空赢过
    本轮的输入。
    """

    if not isinstance(manual_memory_facts, list):
        return []
    items: List[Dict[str, Any]] = []
    for fact in manual_memory_facts:
        if not isinstance(fact, dict):
            continue
        content = str(fact.get("content") or "").strip()
        if not content:
            continue
        fact_id = fact.get("fact_id")
        items.append(
            _make_item(
                category="other",
                value=content,
                params=None,
                source="manual_memory",
                confidence="high",
                visibility="user_visible",
                updated_at=_iso(fact.get("created_at")) or updated_at,
                origin_ref=f"memory_facts.{fact_id}" if fact_id is not None else "memory_facts",
            )
        )
    return items


def _map_preset_constraints(
    preset_pack_constraints: Any, updated_at: str
) -> List[Dict[str, Any]]:
    """preset：``category -> 原文`` 直接映射，无 LLM（与 manual_profile 同一形状）。

    映射表在 ``preset/injector.py::PRESET_PACK_CONSTRAINT_CATEGORIES``，那里也解释了
    为什么这两项不再同时出现在 prompt 尾巴上。``visibility`` 是 ``user_visible``：
    用户亲手挑的风格是他显式声明的偏好，不是系统推理，所以它进【本轮统一约束】而不是
    【参考级背景】。但 ``preset`` 不在 ``_EXPLICIT_SOURCES`` 里，所以它**升不到 hard**。
    """

    if not isinstance(preset_pack_constraints, dict):
        return []
    items: List[Dict[str, Any]] = []
    for category, value in preset_pack_constraints.items():
        normalized_category = _normalize_category(category)
        text = str(value or "").strip()
        if not text:
            continue
        items.append(
            _make_item(
                category=normalized_category,
                value=text,
                params=None,
                source="preset",
                confidence="high",
                visibility="user_visible",
                updated_at=updated_at,
                origin_ref=f"preset.constraints.{normalized_category}",
            )
        )
    return items


def _auto_portrait_block(user_profile: Any, updated_at: str) -> Optional[Dict[str, Any]]:
    """auto_portrait：单一参考级软信号块，锁 internal_only、永不升 hard（§A.3）。"""
    portrait = str(getattr(user_profile, "auto_portrait", "") or "").strip()
    if not portrait:
        return None
    return _make_item(
        category="other",
        value=portrait,
        params=None,
        source="auto_portrait",
        confidence="reference",
        visibility="internal_only",
        updated_at=updated_at,
        origin_ref="user_profiles.auto_portrait",
    )


@dataclass
class ConstraintExtractionResult:
    """自由文本约束抽取结果，显式区分 LLM、fallback 与缺失来源。

    ``items`` 是**真正进 pack 的那一份**（一条自由文本 × 一个类别 = 一条），由
    ``_merge_extracted_constraints`` 把两次抽取合起来得到。``llm_items`` 与
    ``fallback_items`` 留下来只为记账：后者今天只装**模型漏掉的那些类别**，
    所以 ``fallback_extracted`` 与它带出的 ``deterministic_fallback_used``
    才真的是「这一轮确定性规则不得不补位」的意思 —— 那是一个**降级**标记，
    而规则无条件全跑时它对任何一句踩中关键词的正常提问都会亮。
    """

    llm_items: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    fallback_items: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    items: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    missing_sources: List[str] = field(default_factory=list)
    partial_reasons: List[str] = field(default_factory=list)

    def items_for(self, source_id: str) -> List[Dict[str, Any]]:
        """这一条自由文本进 pack 的那一份 —— 每个类别只有一条。

        取数点只有这一个，所以「两个产出方各进各的」在结构上没有落脚处。此前这里是
        调用方自己写的 ``extracted_items + fallback_items``：同一句话、同一个类别于是
        产出两条，一条是模型的 canonical 缩写、一条是整段原文，措辞不同，强制力也可能
        不同。合并在 ``_merge_extracted_constraints`` 里做完，这里只是取走结果。
        """
        return list(self.items.get(source_id) or [])

    @property
    def llm_extracted(self) -> int:
        return sum(len(v) for v in self.llm_items.values())

    @property
    def fallback_extracted(self) -> int:
        return sum(len(v) for v in self.fallback_items.values())


class ConstraintSourceLoader:
    """Assemble the sources consumed once by request-contract normalization."""

    def __init__(
        self,
        state: Any,
        fast_llm: Any,
        *,
        user_profile: Any = None,
        manual_memory_facts: Optional[List[Dict[str, Any]]] = None,
        manual_memory_truncated: bool = False,
        memory_facts: Optional[List[Dict[str, Any]]] = None,
        context_status: str,
        missing_source_layers: Optional[List[str]] = None,
        partial_reasons: Optional[List[str]] = None,
        precomputed_free_text_constraints: Optional[
            Dict[str, List[Dict[str, Any]]]
        ] = None,
    ) -> None:
        self.state = state
        self.fast_llm = fast_llm
        self.user_profile = user_profile
        self.manual_memory_facts = manual_memory_facts
        self.manual_memory_truncated = manual_memory_truncated
        self.memory_facts = memory_facts
        self.context_status = context_status
        self.missing_source_layers = missing_source_layers or []
        self.partial_reasons = partial_reasons or []
        self.precomputed_free_text_constraints = precomputed_free_text_constraints

    @classmethod
    def from_loaded(
        cls,
        state: Any,
        fast_llm: Any,
        *,
        user_profile: Any = None,
        manual_memory_facts: Optional[List[Dict[str, Any]]] = None,
        manual_memory_truncated: bool = False,
        memory_facts: Optional[List[Dict[str, Any]]] = None,
        missing_source_layers: Optional[List[str]] = None,
        partial_reasons: Optional[List[str]] = None,
        precomputed_free_text_constraints: Optional[
            Dict[str, List[Dict[str, Any]]]
        ] = None,
    ) -> "ConstraintSourceLoader":
        missing = list(missing_source_layers or [])
        if user_profile is None and "manual_profile" not in missing:
            missing.append("manual_profile")
        if manual_memory_facts is None and "manual_memory" not in missing:
            missing.append("manual_memory")
        if memory_facts is None and "memory_fact" not in missing:
            missing.append("memory_fact")
        status = "complete" if not missing else "partial"
        return cls(
            state,
            fast_llm,
            user_profile=user_profile,
            manual_memory_facts=manual_memory_facts,
            manual_memory_truncated=manual_memory_truncated,
            memory_facts=memory_facts,
            context_status=status,
            missing_source_layers=missing,
            partial_reasons=partial_reasons or [],
            precomputed_free_text_constraints=precomputed_free_text_constraints,
        )

    async def build_pack(self) -> Dict[str, Any]:
        return await build_constraint_pack(
            self.state,
            self.fast_llm,
            user_profile=self.user_profile,
            manual_memory_facts=self.manual_memory_facts,
            manual_memory_truncated=self.manual_memory_truncated,
            memory_facts=self.memory_facts,
            context_status=self.context_status,
            missing_source_layers=self.missing_source_layers,
            partial_reasons=self.partial_reasons,
            precomputed_free_text_constraints=self.precomputed_free_text_constraints,
        )


async def _llm_extract(
    fast_llm: Any, free_items: List[Dict[str, Any]]
) -> tuple[Dict[str, List[Dict[str, Any]]], Optional[str]]:
    """对自由文本来源做单次 Fast 模型结构化抽取（§A.2.2）：仅归类 / 总结 / params。

    失败（无模型 / 调用异常 / 解析失败）→ 返回空 dict + reason，由 fallback / partial meta 承接。
    """
    if fast_llm is None or not free_items:
        return {}, "llm_unavailable" if free_items else None
    payload = [{"id": it["id"], "text": it["text"]} for it in free_items if it.get("text")]
    if not payload:
        return {}, None
    user = (
        "请逐项判断下列文本是否表达个人出行约束并归类。\n输入项：\n"
        + json.dumps(payload, ensure_ascii=False)
        + '\n\n输出 JSON：{"constraints":[{"id":<输入id>,"is_constraint":<bool>,'
        '"category":<类别>,"value":<canonical 简洁约束陈述>,"params":<对象或 null>}]}'
    )
    try:
        raw = await fast_llm.ainvoke(
            [{"role": "system", "content": _EXTRACT_SYSTEM_PROMPT}, {"role": "user", "content": user}]
        )
    except Exception:
        return {}, "llm_call_failed"
    text = raw if isinstance(raw, str) else (getattr(raw, "content", "") or "")
    parsed = safe_parse_json(text, strip_think_tags=True, enable_repair=True, require_fields=("constraints",))
    out: Dict[str, List[Dict[str, Any]]] = {}
    if parsed and isinstance(parsed.get("constraints"), list):
        for c in parsed["constraints"]:
            if isinstance(c, dict) and c.get("id") is not None and c.get("is_constraint"):
                value = str(c.get("value") or "").strip()
                if not value:
                    continue
                out.setdefault(str(c["id"]), []).append({
                    "category": _normalize_category(c.get("category")),
                    "value": value,
                    "params": c.get("params") if isinstance(c.get("params"), dict) else None,
                })
        return out, None
    return {}, "llm_parse_failed"


def deterministic_budget_constraints(text: str) -> List[Dict[str, Any]]:
    """Extract an explicit numeric budget cap without relying on a model.

    This is public because Request Contract normalization and the lower-level
    Constraint Panel must apply the exact same fallback rule.  A model timeout
    must not make a traveller's numeric cap disappear between those layers.
    """

    out: List[Dict[str, Any]] = []
    # Total-budget grammar is the most explicit and must win before duration
    # phrases.  In ``两天一夜，2 名成人，总预算 3000 元`` the old night-first
    # order paired ``一夜`` with the party size and invented a 2-CNY cap.
    patterns = [
        (r"(?:总预算|全程|整趟|整程)[^\d]{0,8}(\d+(?:\.\d+)?)", "total"),
        (r"(\d+(?:\.\d+)?)[^\d]{0,6}(?:总预算|全程|整趟|整程)", "total"),
        (r"(?:每晚|一晚|/晚|每夜|一夜)[^\d]{0,8}(\d+(?:\.\d+)?)", "night"),
        (r"(\d+(?:\.\d+)?)[^\d]{0,6}(?:每晚|一晚|/晚|每夜|一夜)", "night"),
        (r"(?:每天|每日|一天)[^\d]{0,8}(\d+(?:\.\d+)?)", "day"),
        (r"(\d+(?:\.\d+)?)[^\d]{0,6}(?:每天|每日|一天)", "day"),
    ]
    for pattern, per in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        if per != "total":
            nearby = text[max(0, m.start() - 8) : m.end() + 8].casefold()
            money_cues = (
                "预算",
                "费用",
                "花费",
                "开销",
                "人民币",
                "cny",
                "元",
                "块",
                "以内",
                "不超过",
                "不高于",
                "至多",
                "上限",
            )
            if not any(cue in nearby for cue in money_cues):
                continue
        amount = _budget_amount_param(m.group(1))
        if amount and any(h in text.lower() for h in _HARD_HINTS):
            label = {"night": "每晚预算", "day": "每日预算", "total": "总预算"}[per]
            out.append({
                "category": "budget_cap",
                "value": f"{label}不超过 {amount:.0f} CNY",
                "params": {"amount": amount, "currency": "CNY", "per": per},
            })
            break
    return out


def _fallback_allergy(text: str) -> List[Dict[str, Any]]:
    allergens: List[str] = []
    for m in re.finditer(r"(?:对|不能吃|不吃|忌口|避开)([\u4e00-\u9fa5A-Za-z、/（）()]{1,16}?)(?:过敏|食物|菜|餐|。|，|,|$)", text):
        token = m.group(1).strip(" 、/（）()，,。")
        if token and token not in {"食物", "菜", "餐"}:
            allergens.extend([p for p in re.split(r"[、/,，和及]", token) if p.strip()])
    for token in ("花生", "虾", "蟹", "贝类", "坚果", "牛奶", "奶油", "小麦", "麸质", "面筋"):
        if token in text and ("过敏" in text or "不能吃" in text or "避开" in text):
            allergens.append(token)
    allergens = list(dict.fromkeys(a.strip() for a in allergens if a.strip()))
    if not allergens:
        return []
    return [{
        "category": "food_allergy",
        "value": f"食物过敏/禁忌：{'、'.join(allergens)}",
        "params": {"allergens": allergens},
    }]


def _rule_fallback_extract(free_items: List[Dict[str, Any]]) -> ConstraintExtractionResult:
    result = ConstraintExtractionResult()
    for it in free_items:
        text = str(it.get("text") or "")
        found: List[Dict[str, Any]] = []
        found.extend(deterministic_budget_constraints(text))
        found.extend(_fallback_allergy(text))
        if any(k in text for k in (
            "老人", "父母", "腿脚", "少走路", "少步行", "少爬楼梯", "少楼梯",
            "避免爬坡", "少爬坡", "不要暴走", "走不动", "行动不便", "低步行",
            "膝关节", "膝盖", "膝伤", "腿伤", "连续步行",
        )):
            found.append({
                "category": "health_condition" if any(k in text for k in ("膝", "腿伤")) else "elderly_mobility",
                "value": text,
                "params": _canonical_mobility_params(text, None),
            })
        if any(k in text for k in ("孩子", "儿童", "小孩", "亲子")):
            found.append({
                "category": "child_friendly",
                "value": "儿童同行，需要儿童友好安排",
                "params": {"needs": ["child_friendly"]},
            })
        if any(k in text for k in ("不要夜场", "不去夜店", "不去酒吧", "别安排夜场")):
            found.append({
                "category": "child_friendly",
                "value": "儿童同行，避免夜场或成人向活动",
                "params": {"avoid": ["nightlife", "adult_only"]},
            })
        transport_avoid: List[str] = []
        if any(k in text for k in ("不坐夜巴", "不要夜巴", "避免夜巴", "避开夜巴")):
            transport_avoid.append("night_bus")
        if any(k in text for k in ("不要红眼", "不坐红眼", "避免红眼", "避开红眼")):
            transport_avoid.append("red_eye_flight")
        if any(k in text for k in ("少换乘", "不要多次换乘")):
            transport_avoid.append("many_transfers")
        has_transport_window = any(
            re.search(pattern, text)
            for pattern in (
                r"\d{1,2}:\d{2}\s*(?:前|之前)\s*(?:不|不要|不得|禁止).{0,6}(?:出发|发车)",
                r"\d{1,2}:\d{2}\s*(?:后|之后)\s*(?:不|不要|不得|禁止).{0,6}(?:抵达|到达)",
                r"(?:不早于|最早|不晚于|最晚)\s*\d{1,2}:\d{2}",
            )
        )
        avoid_overnight = any(
            token in text
            for token in ("禁过夜", "禁止过夜", "不安排过夜", "不要过夜", "避免过夜", "不过夜交通")
        )
        if transport_avoid or has_transport_window or avoid_overnight:
            found.append({
                "category": "transport_constraint",
                "value": text,
                "params": _canonical_transport_params(text, {"avoid": transport_avoid}),
            })
        local_mode_params = _canonical_transport_mode_params(
            text,
            None,
            require_intent_hint=True,
        )
        has_explicit_local_mode_intent = any(
            hint in text.casefold()
            for hint in (
                *_LOCAL_MODE_PREFER_HINTS,
                *_LOCAL_MODE_AVOID_HINTS,
                *_LOCAL_MODE_LOCK_HINTS,
            )
        )
        if has_explicit_local_mode_intent and local_mode_params and any(
            key in local_mode_params
            for key in (
                "preferred_local_modes",
                "excluded_local_modes",
                "locked_local_mode",
            )
        ):
            found.append(
                {
                    "category": "transport_constraint",
                    "value": text,
                    "params": local_mode_params,
                }
            )
        if any(k in text for k in (
            "必须有电梯", "要有电梯", "需要电梯", "无烟房", "禁烟", "不要青旅",
            "独立卫浴", "独立卫生间", "独卫",
        )):
            avoid = ["hostel"] if "青旅" in text else []
            needs = []
            if any(k in text for k in ("必须有电梯", "要有电梯", "需要电梯", "酒店有电梯", "住宿有电梯")):
                needs.append("elevator")
            if "无烟" in text or "禁烟" in text:
                needs.append("non_smoking")
            if any(k in text for k in ("独立卫浴", "独立卫生间", "独卫")):
                needs.append("private_bath")
            found.append({
                "category": "accommodation_preference",
                "value": text,
                "params": _canonical_accommodation_params(text, {"needs": needs, "avoid": avoid}),
            })
        if any(k in text for k in ("节奏慢", "慢一点", "不要太赶", "别太赶", "每天不要太多", "悠闲")):
            found.append({
                "category": "pace_preference",
                "value": "行程节奏偏慢，避免高密度安排",
                "params": {"pace": "slow", "max_daily_poi": 5},
            })
        if found:
            result.fallback_items[it["id"]] = found
    result.fallback_items = {k: v for k, v in result.fallback_items.items() if v}
    return result


def _merge_extracted_constraints(
    llm_entries: List[Dict[str, Any]], rule_entries: List[Dict[str, Any]]
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """同一条自由文本的两次抽取合成一份：**一个类别一条**。

    Returns:
        ``(进 pack 的那一份, 其中真的由规则补位的那几条)``
    """

    merged: List[Dict[str, Any]] = [dict(entry) for entry in llm_entries]
    by_category: Dict[str, Dict[str, Any]] = {}
    for entry in merged:
        by_category.setdefault(str(entry.get("category") or ""), entry)

    supplemented: List[Dict[str, Any]] = []
    for entry in rule_entries:
        category = str(entry.get("category") or "")
        existing = by_category.get(category)
        if existing is None:
            # 模型压根没提这个类别 —— 规则整条补进来，并记一笔「这一轮规则补过位」。
            appended = dict(entry)
            by_category[category] = appended
            merged.append(appended)
            supplemented.append(appended)
            continue
        # 同一句话、同一个类别：**取值听模型的**（系统提示词逐字要求它产出 canonical
        # 陈述，「可直接作『已考虑 X』标签」；规则写的是整段原文）。params 两边并起来
        # —— 模型最容易漏的就是 typed params，而 params 正是
        # ``connector_mode_requests_from_constraint_pack`` 与 transport_researcher
        # 读的那一半，漏了它这条约束在下游等于不存在。
        existing["params"] = _merge_constraint_params(
            category, existing.get("params"), entry.get("params")
        )
    return merged, supplemented


async def _extract_free_text(fast_llm: Any, free_items: List[Dict[str, Any]]) -> ConstraintExtractionResult:
    """一条自由文本，一个类别，一条约束。

    自由文本约束有两个产出方，而它们的关系此前只写在名字里、没写在代码里：模型抽一遍
    （``_llm_extract``），确定性规则再抽一遍（``_rule_fallback_extract``），然后调用方
    ``extracted_items + fallback_items`` 把两份**一并**塞进 pack。模型写的 value 是它
    自己的 canonical 缩写，规则写的是**整段原文** —— 文本不同，于是同一句话在 pack 里
    是两条、在 prompt 里印两遍，措辞不一样，硬度也可能不一样。这是同一件事定义在多处、
    各有一份取值在抽取管线那一层的版本；它此前被一个与取值无关的去重键盖着（「每种硬度只留一条」），
    把键改成带取值之后就浮出来了。

    **关系定为「补充」，不是「兜底」，尽管名字全是 fallback。** 规则那一遍存在的
    理由，是模型软化或漏掉**结构性硬类别**时把它补回来（路径是：模型只抽出 pace 与
    transport，而「带父母、少走路」的 ``elderly_mobility`` 由规则补）。改成「模型说了
    话就不用规则」会当场删掉一条老人行动力硬约束 —— 那不是修，是把重复换成漏项。

    所以合并按 ``(这一条自由文本, 类别)`` 做，见 ``_merge_extracted_constraints``：
    取值听模型的，typed params 两边并起来，模型没提的类别由规则整条补上。

    ``fallback_items`` 因此只装**规则真的补了位**的那几条：它连着
    ``partial_reasons`` 里的 ``deterministic_fallback_used``，而那是一个**降级**标记。
    规则无条件全跑时，任何一句踩中关键词的正常提问都会把这一轮标成降级，于是这个标记
    再也分不出「模型挂了」和「模型好好的」。
    """

    llm_items, reason = await _llm_extract(fast_llm, free_items)
    rule_items = _rule_fallback_extract(free_items).fallback_items

    result = ConstraintExtractionResult(llm_items=llm_items)
    for it in free_items:
        source_id = it["id"]
        merged, supplemented = _merge_extracted_constraints(
            list(llm_items.get(source_id) or []), list(rule_items.get(source_id) or [])
        )
        if merged:
            result.items[source_id] = merged
        if supplemented:
            result.fallback_items[source_id] = supplemented
        if not merged:
            result.missing_sources.append(source_id)

    if reason:
        result.partial_reasons.append(reason)
    if result.fallback_extracted:
        result.partial_reasons.append("deterministic_fallback_used")
    return result


def merge_precomputed_constraint_extraction(
    free_items: List[Dict[str, Any]],
    model_items: Dict[str, List[Dict[str, Any]]],
) -> ConstraintExtractionResult:
    """Merge one shared request-normalization result with deterministic safeguards."""
    rule_items = _rule_fallback_extract(free_items).fallback_items
    result = ConstraintExtractionResult(llm_items=model_items)
    for item in free_items:
        source_id = item["id"]
        merged, supplemented = _merge_extracted_constraints(
            list(model_items.get(source_id) or []),
            list(rule_items.get(source_id) or []),
        )
        if merged:
            result.items[source_id] = merged
        if supplemented:
            result.fallback_items[source_id] = supplemented
        if not merged:
            result.missing_sources.append(source_id)
    if result.fallback_extracted:
        result.partial_reasons.append("deterministic_fallback_used")
    return result


def _merge_constraint_params(
    category: str, left: Optional[Dict[str, Any]], right: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """把同一条约束的两次抽取所得的 typed params 并成一份，取**更紧**的那一版。

    合并策略只有这一处定义，两个调用方读的是同一份：

    * ``_merge_extracted_constraints``：同一条自由文本、同一个类别，模型抽了一遍、
      确定性规则又抽了一遍 —— 详略不同，但说的是同一件事。
    * ``_dedupe_constraints``：两条不同来源的 item 落在同一个语义键上。

    「更紧」逐字段有定义：步行分钟取 min、布尔取 or、清单取并集、最早出发取 max、
    最晚抵达取 min。这不是随手写的口径 —— 反过来取会让两次抽取合出一条**比用户
    两句话都松**的约束，而那正是「合并」最容易悄悄干的坏事。
    """

    merged = dict(left or {})
    for key, value in (right or {}).items():
        if key == "max_continuous_walk_minutes":
            incoming_walk = _positive_int_param(value)
            if incoming_walk is None:
                continue
            existing_walk = _positive_int_param(merged.get(key))
            merged[key] = (
                min(existing_walk, incoming_walk)
                if existing_walk is not None
                else incoming_walk
            )
            continue
        if isinstance(value, list):
            merged[key] = list(dict.fromkeys([*(merged.get(key) or []), *value]))
        elif key == "earliest_departure_local" and merged.get(key):
            merged[key] = max(str(merged[key]), str(value))
        elif key == "latest_arrival_local" and merged.get(key):
            merged[key] = min(str(merged[key]), str(value))
        elif isinstance(value, bool):
            merged[key] = bool(merged.get(key)) or value
        elif value is not None:
            merged[key] = value
    if category == "transport_constraint":
        return _canonical_transport_params("", merged)
    if category == "accommodation_preference":
        return _canonical_accommodation_params("", merged)
    if category in {"elderly_mobility", "health_condition"}:
        return _canonical_mobility_params("", merged)
    if category == "food_allergy":
        # 合并之后也得守住「清单与『未知』互斥」：两条 item 并起来之后有了过敏原，
        # 那条「不知道」的标记就不再成立（``value`` 由调用方各自留着，这里只看 params）。
        return _canonical_allergy_params("", merged)
    return merged or None


def _item_source(item: Dict[str, Any]) -> str:
    """这条 item 的来源层（``source_refs`` 里第一条的 source）。"""
    for ref in item.get("source_refs") or []:
        if isinstance(ref, dict) and ref.get("source"):
            return str(ref["source"])
    return ""


def _resolve_conflicts(constraints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """新旧冲突确定性仲裁（§A.4）：单值类取新，同一时刻按来源优先级，旧者 superseded。

    排序键第二位是来源优先级，**这一位不许省**：一次装配里 manual_profile / preset /
    current_query 共享同一个 ``now``，只按 ``updated_at`` 排的话打平之后靠的是
    「谁先被 append」——于是「用户为这一趟挑的风格」与「用户的常设默认」谁说了算，
    答案写在 ``build_constraint_pack`` 里语句的先后顺序上，而不是写在任何一条规则里。
    """
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for c in constraints:
        by_cat.setdefault(c["category"], []).append(c)
    for cat, group in by_cat.items():
        if cat in _SINGULAR_CATEGORIES and len(group) > 1:
            ordered = sorted(
                group,
                key=lambda item: (
                    item.get("updated_at") or "",
                    _SINGULAR_SOURCE_PRECEDENCE.get(_item_source(item), -1),
                    1 if item.get("type") == "hard" else 0,
                ),
                reverse=True,
            )
            winner, older = ordered[0], ordered[1:]
            winner["supersedes"] = older[0]["constraint_id"] if older else None
            for o in older:
                o["status"] = "superseded"
    return constraints


_KNOWN_ALLERGEN_TOKENS = (
    "花生",
    "虾",
    "蟹",
    "贝类",
    "坚果",
    "牛奶",
    "奶油",
    "小麦",
    "麸质",
    "面筋",
    "海鲜",
    "芒果",
    "鸡蛋",
)


#: ``params.allergen_detail`` 的唯一取值：**有过敏这件事成立，而过敏原是什么系统不知道**。
#: 唯一的产出方是 ``_map_manual_profile``（「我的偏好」里勾了那一个过敏选项）——
#: 一张固定选项表说得出「有过敏」，说不出「花生」。
_ALLERGEN_DETAIL_UNKNOWN = "unknown"

#: 语义键上代表「过敏原不知道」的那一个哨兵。它必须是一个**不可能是过敏原名**的 token：
#: 键上放一个像过敏原的占位词（改前的 ``相关过敏原``）会一路被当成过敏原名印出去。
_UNKNOWN_ALLERGEN_KEY = "allergen_detail_unknown"


def _known_allergen_tokens(text: str) -> set[str]:
    return {token for token in _KNOWN_ALLERGEN_TOKENS if token in str(text or "")}


def _allergy_declares_unknown_detail(value: Any, params: Any) -> bool:
    """这条过敏 item 是不是「有过敏、过敏原未知」那一种。**这条判断只在这里写一次。**

    三个条件缺一不可，而第三条正是它存在的理由：``allergen_detail`` 这个标记只表示
    「产出它的那一层手上没有过敏原」，**它不许压过一个真的点了名的过敏原**。否则
    「过敏原是花生」会被一个标记降级成「不知道」，而下游据此告诉模型「别猜过敏原」。
    """

    typed = params if isinstance(params, dict) else {}
    if str(typed.get("allergen_detail") or "") != _ALLERGEN_DETAIL_UNKNOWN:
        return False
    if any(str(token).strip() for token in typed.get("allergens") or []):
        return False
    return not _known_allergen_tokens(str(value or ""))


def _canonical_allergy_params(value: str, params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """``food_allergy`` 的 typed params 归一：过敏原清单与「细节未知」标记**互斥**。

    两条路都产 ``food_allergy``（界面勾的那一项 / 对话与记忆里的 LLM 抽取），归一在这里
    保证它们的产出**结构上分得开**：点了名的进 ``allergens``，没点名的只留
    ``allergen_detail=unknown``。一条 item 同时带两样就是在说「我不知道过敏原，它叫花生」。
    """

    normalized = dict(params or {})
    allergens = [
        token
        for token in dict.fromkeys(str(raw).strip() for raw in normalized.get("allergens") or [])
        if token
    ]
    if allergens:
        normalized["allergens"] = allergens
    else:
        normalized.pop("allergens", None)
    if not _allergy_declares_unknown_detail(value, normalized):
        normalized.pop("allergen_detail", None)
    return normalized or None


def _allergy_allergen_key(item: Dict[str, Any]) -> frozenset[str]:
    """Normalize allergen tokens for food_allergy semantic dedupe.

    LLM may emit ``["花生"]`` while rule fallback emits ``["花生严重", "花生"]``
    for the same sentence; collapse both onto known allergen tokens.

    「有过敏、过敏原未知」自己占一个键（``_UNKNOWN_ALLERGEN_KEY``），**不与任何点了名的
    过敏原合并**：勾了那一项、又在对话里说了「对花生过敏」的用户，两条都要活着走出去重 ——
    合并成「花生」会把「可能还有别的过敏原」这件事说没了，反过来合并成「未知」会把已经
    知道的花生说没了。同一个用户只有一个那一项，所以这个键最多产出一条。
    """
    if _allergy_declares_unknown_detail(item.get("value"), item.get("params")):
        return frozenset({_UNKNOWN_ALLERGEN_KEY})
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    raw = params.get("allergens") if isinstance(params, dict) else None
    blobs: list[str] = []
    if isinstance(raw, list):
        blobs.extend(str(t) for t in raw if str(t).strip())
    blobs.append(str(item.get("value") or ""))
    found: set[str] = set()
    for blob in blobs:
        found |= _known_allergen_tokens(blob)
    if found:
        return frozenset(found)
    # 点了名、但名字不在上面那张表里（`芥末` / `荞麦` / `芹菜`…—— 那张表只有十三个 token，
    # 它是**去重用的归一表**，不是「全部过敏原」）。此时用 ``allergens`` 里的原名，
    # 不要落到下面那条按 ``value`` 洗出来的键上：那条键会把**整句话**当成过敏原名一路印下去，
    # 实测 ``{"allergens": ["芥末"], "value": "芥末过敏"}`` 得到
    # 「芥末过敏过敏，请点餐时避开含芥末过敏的菜品」。同族同一个形状（占位词被印成过敏原名），
    # 与 ``相关过敏原`` 那一处一起改。
    named = frozenset(
        token for token in (str(t).strip() for t in (raw if isinstance(raw, list) else [])) if token
    )
    if named:
        return named
    # Unknown allergen wording: fall back to scrubbed value so identical copy still merges.
    scrubbed = re.sub(r"[\s：:，,。；;、/\\\-_]", "", str(item.get("value") or "").lower())
    # 连一个字都没有的时候，「过敏原是什么」同样是不知道 —— 走上面那一条哨兵，
    # 而不是一个空集（空集在两个消费方那里都得再各自决定一次它是什么意思）。
    return frozenset({scrubbed}) if scrubbed else frozenset({_UNKNOWN_ALLERGEN_KEY})


def _allergen_detail_is_unknown(item: Dict[str, Any]) -> bool:
    """交付面与 prompt 两处共用的那一问：这条过敏**点得出过敏原的名字吗**。

    答案只从语义键上读，不再各自判一次 —— 「模型读到的那句话」与「行程上印出来的那句话」
    对同一条 item 给出不同答案，正是这一族缺陷的形状。
    """

    return _allergy_allergen_key(item) == frozenset({_UNKNOWN_ALLERGEN_KEY})


def _dedupe_constraints(constraints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def semantic_family(item: Dict[str, Any]) -> str:
        category = str(item.get("category") or "")
        return "mobility" if category in {"elderly_mobility", "health_condition"} else category

    def semantic_key(item: Dict[str, Any]) -> tuple[Any, ...]:
        """两条 item 是「同一句话说了两遍」还是「两个互相冲突的答案」。

        去重只负责前者。后者是 ``_resolve_conflicts`` 的活 —— 单值类
        （``_SINGULAR_CATEGORIES``）要按 recency 与来源优先级裁一次，输的那条标
        ``superseded`` 而不是消失。**所以不同的取值必须活着走出去重**，否则那套仲裁
        结构上永远跑不到，而「谁说了算」的答案会落在谁先被 append 上。

        由此得到这里唯一的规则：**键必须带上这条 item 断言的取值**。取值就是它的
        ``value`` 文本，除非某一族刻意把取值从文本里提炼了出来 —— ``food_allergy``
        提炼成过敏原集合、``budget_cap`` 提炼成金额 —— 那就用提炼出来的那个。

        ``params`` **不是身份**，是取值的派生载荷：同一句话被 LLM 与规则各抽一遍、
        抽出的详略不同，那仍是同一句话，该合并而不是变成两行；``merge_params`` 负责
        把两次抽取并起来。反过来，两句不同的话即使抽出同一份 params 也是两条。

        这个键曾经在四个地方把不同取值折成一条：

        * ``budget_cap`` 的键只有 ``(currency, per)``，于是「预算档位：舒适」与
          「经济」同键 —— 两个不同档位合成一条，先进来的静默胜出。数值上限那一档必须
          仍然合并（同一个金额说两遍是重复），所以键里要带 ``amount``，而没有数值时
          改用取值本身。
        * 无 params 的类别本想退回按取值比，写的是 ``semantic_params or normalized_value``
          —— 而 ``json.dumps({})`` 是 ``"{}"``，**非空字符串**，``or`` 的右支因此
          从来不执行：所有 paramless 的 ``pace_preference`` 共用一个键。
        * ``transport_constraint`` / ``accommodation_preference`` 的键是
          ``(family, item["type"])`` —— 即「每种硬度只留一条」，与取值完全无关。
          勾了「高铁、公共交通、步行」三个交通偏好，出来的是**一条拼接体**：
          ``merge_item`` 留下先到的 ``value="高铁"``，却并进了后到的
          ``params={"preferred_local_modes": ["walk"]}``。而 ``params`` 正是
          ``connector_mode_requests_from_constraint_pack`` 与 transport_researcher
          读的那一半 —— 用户选了高铁，系统按「只走路」去要连接段，「公共交通」整条消失。
        * 有 params 的类别按 ``params`` 的 JSON 比 —— 同一个形状：两句不同的话只要
          抽出的 params 撞上就合成一条，先到的那句静默胜出。

        ``mobility``（``elderly_mobility`` / ``health_condition``）是唯一保留折叠的一族，
        因为它不是一张可选清单，而是**同行者的一条行动力包线**：几句话在描述同一件事，
        ``merge_params`` 把它折成最紧的那一版（步行分钟取 min、避楼梯取 or、prefer 取并集），
        而这一族的 ``_public_summary`` 完全由 ``params`` 算出、从不读 ``value``，所以
        折叠折不出「说的是 A、参数是 B」。键里保留 ``type``：一句参考级的行动力提示与
        一条硬性要求不是同一条包线，合并会把前者静默升成后者。
        """

        family = semantic_family(item)
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        normalized_value = re.sub(
            r"[\s：:，,。；;、/\\\-_]", "", str(item.get("value") or "").lower()
        )
        if family == "food_allergy":
            return family, _allergy_allergen_key(item)
        if family == "mobility":
            return family, item.get("type")
        if family == "budget_cap":
            amount = params.get("amount")
            return (
                family,
                params.get("currency"),
                params.get("per"),
                amount if amount is not None else normalized_value,
            )
        return family, normalized_value

    def merge_item(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        existing["params"] = _merge_constraint_params(
            str(existing.get("category") or ""),
            existing.get("params") or {},
            incoming.get("params") or {},
        )
        existing["source_refs"] = [
            *existing.get("source_refs", []),
            *[
                ref
                for ref in incoming.get("source_refs", [])
                if ref not in existing.get("source_refs", [])
            ],
        ]
        if incoming.get("type") == "hard":
            existing["type"] = "hard"
            existing["priority"] = "hard"
        if incoming.get("visibility") == "user_visible":
            existing["visibility"] = "user_visible"
        existing["candidate_worker_kinds"] = list(dict.fromkeys([
            *existing.get("candidate_worker_kinds", []),
            *incoming.get("candidate_worker_kinds", []),
        ]))
        existing["enforcement_scope"] = list(dict.fromkeys([
            *existing.get("enforcement_scope", []),
            *incoming.get("enforcement_scope", []),
        ]))
        existing["public_summary"] = _public_summary(
            str(existing.get("category") or ""),
            str(existing.get("value") or ""),
            existing.get("params"),
        )
        return existing

    index_by_key: Dict[tuple[Any, ...], int] = {}
    out: List[Dict[str, Any]] = []
    for c in constraints:
        key = semantic_key(c)
        if key in index_by_key:
            merge_item(out[index_by_key[key]], c)
        else:
            index_by_key[key] = len(out)
            out.append(c)
    return out


def constraint_free_text_sources(
    state: Any,
    memory_facts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    now = _now_iso()
    free_items: List[Dict[str, Any]] = []
    anchor = _get(state, "session_anchor") or {}
    if isinstance(anchor, dict):
        for index, value in enumerate(anchor.get("key_constraints") or []):
            free_items.append(
                {
                    "id": f"anchor_{index}",
                    "source": "session_anchor",
                    "text": str(value),
                    "updated_at": anchor.get("compressed_at") or now,
                }
            )
    user_query = str(_get(state, "user_query") or "").strip()
    if user_query:
        free_items.append(
            {
                "id": "query_main",
                "source": "current_query",
                "text": user_query,
                "updated_at": now,
            }
        )
    plan_amendment = _get(state, "plan_gate_amendment")
    if plan_amendment is not None:
        content = str(getattr(plan_amendment, "content", "") or "").strip()
        command_id = str(
            getattr(plan_amendment, "command_id", "plan_gate_amendment")
        )
        if content:
            free_items.append(
                {
                    "id": command_id,
                    "source": "plan_gate_amendment",
                    "text": content,
                    "updated_at": now,
                }
            )
    for amendment in _get(state, "pending_intent_amendments") or []:
        content = str(getattr(amendment, "content", "") or "").strip()
        command_id = str(getattr(amendment, "command_id", "") or "").strip()
        if content and command_id:
            free_items.append(
                {
                    "id": command_id,
                    "source": "run_supplement",
                    "text": content,
                    "updated_at": now,
                }
            )
    for index, fact in enumerate(memory_facts or []):
        if isinstance(fact, dict):
            free_items.append(
                {
                    "id": f"fact_{index}",
                    "source": "memory_fact",
                    "text": str(fact.get("content") or ""),
                    "updated_at": _iso(fact.get("created_at")) or now,
                    "category_meta": fact.get("category"),
                    "importance": int(fact.get("importance") or 0),
                }
            )
    return free_items


async def build_constraint_pack(
    state: Any,
    fast_llm: Any,
    *,
    user_profile: Any = None,
    manual_memory_facts: Optional[List[Dict[str, Any]]] = None,
    manual_memory_truncated: bool = False,
    memory_facts: Optional[List[Dict[str, Any]]] = None,
    context_status: str = "unknown",
    missing_source_layers: Optional[List[str]] = None,
    partial_reasons: Optional[List[str]] = None,
    precomputed_free_text_constraints: Optional[
        Dict[str, List[Dict[str, Any]]]
    ] = None,
) -> Dict[str, Any]:
    """组装 PersonalConstraintPack（约束侧唯一事实源，一处装配多处消费，JP-02-02 §A.0）。

    ``manual_memory_truncated`` 是取数那一侧的回答：「``manual_memory_facts`` 是用户
    写下的全部，还是被条数上限切过一刀的前 N 条」。**这不是一个可以省略的细节** ——
    少了它，一份切过的清单和一份完整的清单在这里长得一模一样，于是 prompt 会把
    「用户只写了这几条」和「用户写了更多、多出来的没进来」印成同一个样子。
    上限那个数不在这里（见 ``_manual_memory_omitted_note``），这里只要一个是非。
    """
    run_id = _get(state, "run_id") or ""
    now = _now_iso()
    constraints: List[Dict[str, Any]] = []
    source_layers: List[str] = []

    # 1) manual_profile（结构直映）+ 5) auto_portrait（单一参考块）。
    if user_profile is not None:
        mapped = _map_manual_profile(user_profile, now)
        if mapped:
            constraints.extend(mapped)
            source_layers.append("manual_profile")
        portrait = _auto_portrait_block(user_profile, now)
        if portrait is not None:
            constraints.append(portrait)
            source_layers.append("auto_portrait")

    # 1b) manual_memory（结构直映，无 LLM）：「个性记忆」里手写的每一条，原文进偏好段。
    # 排在 manual_profile 之后、自由文本抽取之前，是因为偏好那一档被预算截断时按录入
    # 顺序留前面的，而这一层是用户亲手写下、要求长期带着的规则。
    manual_memory_items = _map_manual_memory_facts(manual_memory_facts, now)
    if manual_memory_items:
        constraints.extend(manual_memory_items)
        source_layers.append("manual_memory")

    # 6) preset（结构直映，无 LLM）：用户为这一趟挑的风格里的节奏与预算档位。
    # 它与 manual_profile 争的正是 pace_preference / budget_cap 这两个单值类，
    # 由 ``_resolve_conflicts`` 按来源优先级裁决（preset > manual_profile）。
    preset_items = _map_preset_constraints(_get(state, "preset_pack_constraints"), now)
    if preset_items:
        constraints.extend(preset_items)
        source_layers.append("preset")

    # 2) session_anchor / 3) current_query / 4) memory_fact → 自由文本 LLM 抽取。
    free_items = constraint_free_text_sources(state, memory_facts)
    extraction = (
        merge_precomputed_constraint_extraction(
            free_items, precomputed_free_text_constraints
        )
        if precomputed_free_text_constraints is not None
        else await _extract_free_text(fast_llm, free_items)
    )
    seen_sources: set = set()
    for it in free_items:
        # 一条自由文本只取一份（``items_for``：模型说了算，模型没说话才轮到规则）。
        # 此前这里是 ``extracted_items + fallback_items`` —— 两个产出方永远同时进 pack。
        extracted = extraction.items_for(it["id"])
        if not extracted:
            continue
        source = it["source"]
        if source == "memory_fact":
            high = it.get("category_meta") == "constraint" and it.get("importance", 0) >= 8
            confidence = "high" if high else "medium"
            visibility = "user_visible" if high else "internal_only"
        else:  # session_anchor / current query / amendment：显式来源
            confidence = "high"
            visibility = "user_visible"
        source_text = str(it.get("text") or "").strip()
        for ex in extracted:
            # An extraction that hands back its own input is not an extraction.
            # Batch 4 measured the consequence on the very first screen of the
            # product: a plan gate whose 「本轮必须遵守」 list printed the user's
            # whole request back at them —
            # ``public_summary = "帮我规划 2026-08-10 到 2026-08-12 从上海出发去苏州的
            # 行程。同行有一位 78 岁…"``.  It gets there because ``_public_summary``
            # falls through to ``value`` for any category without its own
            # formatter, and ``value`` was the entire ``query_main`` text.
            # The guard belongs here, at the one boundary where free text becomes
            # a constraint, so it covers every free-text source (query, brief,
            # session anchor, memory fact) rather than one screen's formatter.
            value = str(ex["value"] or "").strip()
            # Only drop it when that paragraph would actually **reach the screen**.
            # Categories with their own formatter (elderly_mobility, budget_cap with
            # an amount, transport_constraint, …) summarise from ``params`` and never
            # print ``value``, so a whole-input value there is harmless -- and
            # dropping it would throw away a real source ref, which is what the
            # first version of this guard did to the deterministic rule fallback.
            if (
                source_text
                and value == source_text
                and len(source_text) > _MAX_EXTRACTED_CONSTRAINT_CHARS
                and _public_summary(ex["category"], value, ex["params"]) == source_text
            ):
                logger.info(
                    "Constraint extraction returned its own input verbatim and it would "
                    "reach the screen, dropped | source=%s origin=%s category=%s chars=%d",
                    source,
                    it["id"],
                    ex["category"],
                    len(source_text),
                )
                continue
            constraints.append(
                _make_item(
                    category=ex["category"],
                    value=ex["value"],
                    params=ex["params"],
                    source=source,
                    confidence=confidence,
                    visibility=visibility,
                    updated_at=it["updated_at"],
                    origin_ref=it["id"],
                )
            )
        seen_sources.add(source)
    for s in seen_sources:
        if s not in source_layers:
            source_layers.append(s)

    constraints = _resolve_conflicts(_dedupe_constraints(constraints))
    active = [c for c in constraints if c["status"] == "active"]
    unsupported_hard = [
        c["constraint_id"]
        for c in active
        if c.get("type") == "hard" and c.get("unsupported_hard_category")
    ]
    free_text_sources_total = len([it for it in free_items if str(it.get("text") or "").strip()])
    extraction_status = {
        "free_text_sources_total": free_text_sources_total,
        "llm_extracted": extraction.llm_extracted,
        "fallback_extracted": extraction.fallback_extracted,
        "missing_sources": list(extraction.missing_sources),
        "partial_reasons": list(dict.fromkeys((partial_reasons or []) + extraction.partial_reasons)),
    }

    return {
        "pack_meta": {
            "run_id": run_id,
            "user_id": _get(state, "user_id") or "",
            "session_id": _get(state, "session_id") or "",
            "built_at": now,
            "pack_version": "v2",
            "source_layers": source_layers,
            "constraint_context_status": context_status,
            "missing_source_layers": list(missing_source_layers or []),
            "manual_memory_truncated": bool(manual_memory_truncated),
            "extraction_status": extraction_status,
            "hard_constraint_contract_complete": not unsupported_hard,
            "unsupported_hard_constraint_ids": unsupported_hard,
        },
        "constraints": constraints,
        "hard_constraints": [c for c in active if c["type"] == "hard"],
        "soft_preferences": [c for c in active if c["type"] == "soft"],
    }


def referenced_context_sections(pack: Any) -> List[Dict[str, Any]]:
    """上下文透镜（「本次参考的信息」）要列的条目 —— 逐条等于 prompt 真印出去的那些。

    这个函数与 ``format_constraint_pack_for_prompt`` 走**同一个产地**
    （``_constraint_prompt_sections``）：同一个 pack、同一套可见性规则、同一组预算。
    这是它存在的全部理由 —— 「屏幕上说参考了」与「模型真的读到了」不可能各自漂移。

    **它此前做不到，因为透镜有一个属于自己的 limit（8）。** prompt 那侧是 12 / 24 / 6
    三段各自的预算，而取值顺序是先 ``user_visible`` 后 ``internal_only``、组内先硬约束
    后偏好、组内按录入顺序 —— 于是用户可见的条目一旦排到第 9 条，后面的与相关性、
    重要度全部无关地上不了榜，而它们在 prompt 里一条不少。六组偏好都填满的用户
    今天恰好就在这个状态。那个 8 现在不存在了：条数由预算层说了算，说一次。

    分三段返回而不是摊平成一串，因为 prompt 里它们本来就是三段不同的东西 ——
    尤其【参考级背景】那一段在 prompt 里明写着「不是约束」，界面把它和用户自己声明的
    那两段混成一串时，系统猜出来的东西看上去就和用户亲口说的一样硬。

    出声那一句（``（本类还有 N 条…未列出…）``）**不在条目里**：它是「有条目没进来」
    的说明，不是一条参考信息。
    """

    if not isinstance(pack, dict):
        return []
    sections: List[Dict[str, Any]] = []
    for section in _constraint_prompt_sections(pack):
        items: List[str] = []
        for row in section["rows"]:
            if row["value"] not in items:
                items.append(row["value"])
        if items:
            sections.append({"key": section["key"], "items": items})
    return sections


#: 「有过敏、过敏原未知」在交付面上的那一句。**它不许出现任何过敏原名，也不许说「已避开」**：
#: 系统手上只有「这个人有食物过敏」这一件事，避开什么、避开没避开都无从判断。
#: 「本行程没有替你排除任何过敏原」这半句是刻意的 —— 一句只提醒不否认的话，读起来就像
#: 「剩下的我们处理了」。
_UNKNOWN_ALLERGEN_DINING_NOTE = (
    "有食物过敏，而系统没有拿到过敏原是什么："
    "本行程没有替你排除任何过敏原，请点餐前向店员说明过敏情况并逐一确认食材。"
)


def dining_allergy_reminders(pack: Any) -> List[str]:
    """Concise dining-local reminders for soft food allergies.

    两种过敏在这里给的是**两句不同的话**，因为系统知道的东西不一样：点得出名字的那一种
    说「避开含 X 的菜品」，点不出名字的那一种（界面上勾的那一项）只能说「有过敏、
    过敏原未知」。把后者也印成前者，需要先编一个过敏原名出来 —— 改前那句
    「请点餐时避开含相关过敏原的菜品」就是这么来的，读者与模型都会以为系统知道该避开什么。
    """
    if not isinstance(pack, dict):
        return []
    allergies: dict[frozenset[str], bool] = {}
    detail_unknown = False
    for item in pack.get("soft_preferences") or []:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in (None, "active"):
            continue
        if str(item.get("category") or "") != "food_allergy":
            continue
        if item.get("visibility") not in (None, "user_visible"):
            continue
        if _allergen_detail_is_unknown(item):
            detail_unknown = True
            continue
        allergens = _allergy_allergen_key(item)
        is_severe = "严重" in str(item.get("value") or "")
        allergies[allergens] = allergies.get(allergens, False) or is_severe

    notes: List[str] = []
    # 「有过敏、细节未知」排在前面：它讲的是整件事的边界（系统不知道过敏原），
    # 而后面每一句讲的是某一个已知过敏原。
    if detail_unknown:
        notes.append(_UNKNOWN_ALLERGEN_DINING_NOTE)
    for allergens, is_severe in allergies.items():
        allergen_text = "、".join(sorted(allergens))
        severity = "严重" if is_severe else ""
        notes.append(
            f"{allergen_text}{severity}过敏，请点餐时避开含{allergen_text}的菜品，"
            "并向店员说明过敏情况。"
        )
    return notes


def _pack_prompt_lines(pack: Dict[str, Any], *, user_visible: bool) -> List[Dict[str, Any]]:
    """把 pack 里符合可见性的 active item 摊平成 (标签, 正文) 待印行。"""
    rows: List[Dict[str, Any]] = []
    for label, key in (("硬约束", "hard_constraints"), ("偏好", "soft_preferences")):
        for item in pack.get(key) or []:
            if not isinstance(item, dict):
                continue
            if (item.get("visibility") == "user_visible") is not user_visible:
                continue
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            # ``source`` 带上来是为了让「这一档里手写记忆真印出去几条」有人能数
            # （见 ``_manual_memory_omitted_note``）—— 数它的地方必须是**预算之后**，
            # 否则报出去的条数和 prompt 里的条数会各自漂。
            rows.append({
                "label": label,
                "category": str(item.get("category") or "other"),
                "value": value,
                # ``params`` 带上来只为一件事：过敏那一行要分「过敏原是 X」与
                # 「有过敏、过敏原未知」（``_render_constraint_line``）。这两种区别**不在
                # ``value`` 文本里**，只在 typed params 里 —— 让渲染层去猜文本就等于给
                # 「系统知不知道过敏原」这个问题开第二个答案。
                "params": item.get("params"),
                "source": _item_source(item),
            })
    return rows


def _render_constraint_line(row: Dict[str, Any]) -> str:
    category, value, label = row["category"], row["value"], row["label"]
    if category == "food_allergy":
        if _allergen_detail_is_unknown(row):
            # 过敏原未知那一种（界面上勾的那一项）。这一段**不许**说「避开相关食材」：
            # 没有食材可指，模型只能自己编一个，编出来的那个会被读者当成系统已经核过的事实。
            # 措辞只说「**这一条**没有附带过敏原清单」，不说「这个人的过敏原全都不知道」——
            # 同一轮里另有一条点了名的过敏（对话里说了花生）时，后者会在下一行印出来，
            # 而一条渲染器看不到别的行。说小的那一句在两种情形下都成立。
            return (
                f"- [用餐提醒/{category}] {value}。"
                f"这一条只说明「有食物过敏」，没有附带过敏原清单："
                f"不许猜过敏原，也不许把任何菜品、餐厅或整份行程说成「已避开过敏原」"
                f"「已确认无过敏原」。正常推荐具体餐厅即可，"
                f"并在用餐安排旁提醒旅客自己向店员说明过敏情况、逐一确认食材。"
            )
        return (
            f"- [用餐提醒/{category}] {value}。"
            f"正常推荐具体餐厅即可；在对应餐馆或用餐安排旁提醒点菜时避开相关食材。"
            f"禁止因过敏拒绝全部餐厅，禁止要求或宣称「全店已确认无过敏原」。"
        )
    return f"- [{label}/{category}] {value}"


def _budgeted_rows(
    rows: List[Dict[str, Any]], budget: int, *, what: str
) -> tuple[List[Dict[str, Any]], str]:
    """一类待印行按自己的预算取前 N 条，被截时给出一句明说。

    **每一类各有一个预算，而不是共用一个池。** 共用一个池时，硬约束整段排在偏好前面，
    所以被截掉的**永远**是偏好：一个偏好多的用户，多出来的那几条从写下来那天起
    没有进过任何 prompt。两个预算把「这一类装不下」变成一件只影响这一类的事。

    **截断必须出声。** 一个静默的上限会把「用户只声明了这几条」和「用户声明了更多、
    多出来的没进 prompt」印成同一个样子，而模型与读日志的人都无从分辨 —— 那正是
    这个仓反复付过代价的形状（合同写了一份、产品用着另一份、中间没有钉）。

    返回的是**条目**与**那句话**两样东西，而不是拼好的一串行：上下文透镜要列的是条目，
    而那句话是「有条目没进来」的说明 —— 混在一起返回，浮窗里就会多出一条用户没写过的
    「参考信息」。

    Returns:
        ``(装得下的行, 出声那一句或空串)``
    """

    if not rows:
        return [], ""
    kept = rows[:budget]
    if len(rows) <= budget:
        return kept, ""
    note = (
        f"（本类还有 {len(rows) - budget} 条{what}未列出：受 prompt 预算限制，"
        f"已按录入顺序取前 {budget} 条。未列出的不等于用户撤回了它们。）"
    )
    return kept, note


def _manual_memory_omitted_note(kept_rows: List[Dict[str, Any]]) -> str:
    """手写记忆撞上**条数上限**时的那一句明说（行数预算那一道见 ``_budgeted_rows``）。

    这两道是不同的门，所以出声也是两句：``_budgeted_rows`` 管的是「这一档 prompt 装
    不下这么多行」，这里管的是「记忆层压根没把那么多条取回来」。前者知道差几条，
    后者不知道 —— 而它们可以同时发生。

    **上限本身不在这里，也不该在这里。** 它只有一处定义
    （``memory/context_builder.py::ContextBudget.manual_memory_facts_limit``），
    由 Constraint Pack 的统一装配入口在取数时用掉。这一层只被告知
    「被截了」这件事，以及本轮真印出去几条 —— 把那个数搬进来就等于让它有两处定义。

    **不报「少了几条」这个确数。** 取数那一侧多取一条当**探针**（``limit + 1``），
    探针只证明「后面还有」，不证明还有几条；报一个偏小的确数比不报数更糟。

    条数从**预算之后**的 ``kept_rows`` 里数：说出去的那个数必须就是 prompt 里
    真印出去的那个数，否则「印了几条」在同一份文本里有两个答案。
    """

    printed = sum(1 for row in kept_rows if row.get("source") == "manual_memory")
    return (
        f"（还有更早写下的手写记忆未列出：受 prompt 预算限制，本轮只注入了最近写下的 "
        f"{printed} 条。未列出的不等于用户撤回了它们。）"
    )


def _constraint_prompt_sections(pack: Dict[str, Any]) -> List[Dict[str, Any]]:
    """本轮 pack 会印进 prompt 的三段 —— prompt 与上下文透镜的**同一个产地**。

    两个消费方（``format_constraint_pack_for_prompt`` 与
    ``referenced_context_sections``）在这里分叉，分叉点在**预算之后**：条数、顺序、
    可见性分组都已经定死，谁也没有机会再加一道自己的截断。

    ``omitted_notes`` 是**一串**而不是一句：一档里可能同时有两道门咬到人（记忆层的
    条数上限先截了一次，prompt 的行数预算又截了一次），把它们挤成一句就得挑一句
    不说 —— 而没说出来的那一道，正是这一族缺陷本身的形状。
    """

    declared = _pack_prompt_lines(pack, user_visible=True)
    grouped = {
        "hard": [row for row in declared if row["label"] == "硬约束"],
        "preference": [row for row in declared if row["label"] == "偏好"],
        "reference": _pack_prompt_lines(pack, user_visible=False),
    }
    budgets = (
        ("hard", _PROMPT_HARD_LINES, "硬约束"),
        ("preference", _PROMPT_PREFERENCE_LINES, "偏好"),
        ("reference", _PROMPT_REFERENCE_LINES, "参考级背景"),
    )
    meta = pack.get("pack_meta")
    manual_memory_truncated = bool(isinstance(meta, dict) and meta.get("manual_memory_truncated"))
    sections: List[Dict[str, Any]] = []
    for key, budget, what in budgets:
        kept, note = _budgeted_rows(grouped[key], budget, what=what)
        notes = [note] if note else []
        # 手写记忆是用户显式声明的偏好（``_map_manual_memory_facts`` 恒给 soft +
        # user_visible），所以它只可能落在这一档。
        if key == "preference" and manual_memory_truncated:
            notes.append(_manual_memory_omitted_note(kept))
        sections.append({"key": key, "rows": kept, "omitted_notes": notes})
    return sections


def format_constraint_pack_for_prompt(pack: Any) -> str:
    """Project the shared pack into the Agent instructions that reach the model.

    这个函数是**两条路径上记忆抵达模型的唯一通道**。``memory/context_builder.py``
    曾经也装配同样的层，而它的产出没有任何 prompt 消费方，那半边后来删掉了，所以
    「哪一层进不进 prompt」这件事今天只在这里决定一次，别处不要再开第二条注入路径 ——
    再开一条，同一条手写记忆就会在两节措辞不同的段落里各印一遍。

    两类可见性都印，且印成两节不同的东西：

    * ``user_visible`` → 【本轮统一约束】。用户自己声明过的。硬约束那一半门会读；
      偏好那一半（勾出来的偏好、挑的风格、手写的记忆）只影响取舍，不构成准入门槛。
    * ``internal_only`` → 【参考级背景】。系统从历史对话推理出来的画像、以及重要度
      没到 high 档的记忆事实。**此前这一类零消费方**：全仓只有两处读 ``visibility``，
      两处都是 ``== "user_visible"``，于是画像与大多数记忆事实每轮被读出库、花一次
      fast 模型调用做结构化抽取、去重、消解冲突、存进 state —— 然后没有任何模型、
      没有任何门看过一眼。代码注释管它叫「参考级软信号块」，而代码里并不存在
      「参考级」这个档，只有「印」和「不印」。这一节就是那个档，它必须在 prompt 里
      **明说自己不是约束**，否则「参考级」会变成一条模型分不清的硬要求。

    参考级永不升 hard、门永不据它拦截：``entities/itinerary_composition_v2.py`` 那个
    把 transport_constraint 投影成 connector mode request 的读取点仍然只认
    ``user_visible``，本函数没有改动那条线。
    """
    if not isinstance(pack, dict):
        return ""

    sections = {section["key"]: section for section in _constraint_prompt_sections(pack)}

    def _render(key: str) -> List[str]:
        section = sections[key]
        lines = [_render_constraint_line(row) for row in section["rows"]]
        lines.extend(section["omitted_notes"])
        return lines

    parts: List[str] = []
    declared_lines = _render("hard") + _render("preference")
    if declared_lines:
        parts.append("【本轮统一约束】\n" + "\n".join(declared_lines))

    reference_lines = _render("reference")
    if reference_lines:
        parts.append(
            "【参考级背景 — 不是约束】\n"
            "以下是系统根据历史对话推理出的用户特征、以及重要度未达高档的历史记忆，"
            "用户并未在本轮声明它们。可以用来在几个都合规的选项之间排序与取舍；"
            "不得当成硬性要求，不得据此拒绝任何候选或缩小调研范围，"
            "与上面【本轮统一约束】冲突时一律以上面为准。\n"
            + "\n".join(reference_lines)
        )

    return "\n\n".join(parts)

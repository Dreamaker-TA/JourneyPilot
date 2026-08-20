"""Normalize request clauses into strict intent and constraint drafts."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Literal, Optional

from openai import OpenAIError
from pydantic import Field, ValidationError, model_validator

from ..entities.delivery_bundle import StrictModel
from ..entities.intent_spec import (
    AlternativeIntentValue,
    CadenceIntentValue,
    CategoryIntentValue,
    CountIntentValue,
    IntentImpactStage,
    IntentKind,
    IntentStrength,
    IntentTarget,
    IntentValue,
    OutputRequirementValue,
    ScalarIntentValue,
    VerificationMode,
)
from ..entities.request_contract import ClauseDisposition
from ..models.strict_json_schema import as_strict_schema
from ..panels.constraint import deterministic_budget_constraints
from ..utils.json_helpers import safe_parse_json


INTENT_NORMALIZATION_PROMPT_VERSION = "request_contract_normalization.v1"
# A request contract is a bounded clause ledger, not a long-form answer.  Its
# ceiling still needs room for every clause and the provider's structured
# planning tokens; the operation budget below prevents that headroom from
# turning a slow completion into an unbounded pre-approval wait.
INTENT_NORMALIZATION_OUTPUT_TOKENS = 8192
INTENT_NORMALIZATION_CALL_TIMEOUT_SECONDS = 120.0
INTENT_NORMALIZATION_OPERATION_TIMEOUT_SECONDS = 240.0

logger = logging.getLogger(__name__)


class ConstraintParamsDraft(StrictModel):
    amount: Optional[float] = None
    currency: Optional[str] = None
    per: Optional[Literal["night", "day", "total"]] = None
    allergens: List[str] = Field(default_factory=list)
    avoid_overnight: Optional[bool] = None
    earliest_departure_local: Optional[str] = None
    latest_arrival_local: Optional[str] = None
    preferred_local_modes: List[str] = Field(default_factory=list)
    excluded_local_modes: List[str] = Field(default_factory=list)
    locked_local_mode: Optional[str] = None
    required_facilities: List[str] = Field(default_factory=list)
    max_continuous_walk_minutes: Optional[int] = Field(default=None, ge=1)
    avoid_long_stairs: Optional[bool] = None
    prefer: List[str] = Field(default_factory=list)
    unknown_facility_policy: Optional[Literal["needs_confirmation"]] = None

    def compact(self) -> Dict[str, object]:
        return self.model_dump(exclude_none=True, exclude_defaults=True)


class NormalizedConstraintDraft(StrictModel):
    category: Literal[
        "food_allergy",
        "budget_cap",
        "elderly_mobility",
        "child_friendly",
        "transport_constraint",
        "accommodation_preference",
        "pace_preference",
        "destination_preference",
        "dietary_restriction",
        "health_condition",
        "other",
    ]
    value: str = Field(min_length=1, max_length=300)
    params: ConstraintParamsDraft = Field(default_factory=ConstraintParamsDraft)


class IntentDraft(StrictModel):
    kind: IntentKind
    target: IntentTarget
    strength: IntentStrength
    priority: int = Field(ge=0, le=100)
    value: IntentValue
    verification_mode: VerificationMode
    impact_stages: List[IntentImpactStage] = Field(min_length=1)
    public_summary: str = Field(min_length=1, max_length=300)


class NormalizedClauseDraft(StrictModel):
    clause_id: str = Field(min_length=1)
    disposition: ClauseDisposition
    reason_code: Optional[str] = None
    intents: List[IntentDraft] = Field(default_factory=list)
    constraints: List[NormalizedConstraintDraft] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mapping(self) -> "NormalizedClauseDraft":
        if self.disposition is ClauseDisposition.MAPPED_TO_INTENT and not self.intents:
            raise ValueError("mapped clause requires an intent draft")
        if (
            self.disposition is ClauseDisposition.MAPPED_TO_CONSTRAINT
            and not self.constraints
        ):
            raise ValueError("constraint-mapped clause requires a constraint draft")
        if self.disposition is not ClauseDisposition.MAPPED_TO_INTENT and self.intents:
            raise ValueError("only mapped clauses may contain intent drafts")
        if (
            self.disposition
            in {
                ClauseDisposition.UNSUPPORTED,
                ClauseDisposition.UNRESOLVED,
            }
            and not self.reason_code
        ):
            raise ValueError("unsupported or unresolved clause requires a reason code")
        return self


class RequestContractNormalizationResult(StrictModel):
    clauses: List[NormalizedClauseDraft] = Field(min_length=1)


@dataclass(frozen=True)
class SourceClause:
    clause_id: str
    source_ref_id: str
    source_kind: str
    source_text: str
    span_start: int
    span_end: int


_BOUNDARY = re.compile(r"[^，,。；;！？!?\n]+")
_MATERIAL_CUES = (
    "要",
    "不要",
    "不能",
    "必须",
    "安排",
    "规划",
    "每天",
    "每个",
    "最多",
    "至少",
    "重点",
    "解释",
    "方案",
    "avoid",
    "must",
    "every",
    "at most",
    "at least",
)


def split_source_clauses(
    sources: Iterable[tuple[str, str, str]],
) -> List[SourceClause]:
    clauses: List[SourceClause] = []
    for source_ref_id, source_kind, text in sources:
        for match in _BOUNDARY.finditer(text or ""):
            raw = match.group(0)
            stripped = raw.strip()
            if not stripped:
                continue
            leading = len(raw) - len(raw.lstrip())
            start = match.start() + leading
            end = start + len(stripped)
            digest = _short_hash(source_ref_id, str(start), str(end), stripped)
            clauses.append(
                SourceClause(
                    clause_id=f"clause_{digest}",
                    source_ref_id=source_ref_id,
                    source_kind=source_kind,
                    source_text=stripped,
                    span_start=start,
                    span_end=end,
                )
            )
    return clauses


def is_material_clause(text: str) -> bool:
    lowered = text.lower()
    return any(cue in lowered for cue in _MATERIAL_CUES)


def _short_hash(*parts: str) -> str:
    from hashlib import sha256

    return sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]


def _normalization_prompt(
    clauses: List[SourceClause], controlled_identity: Dict[str, object]
) -> List[Dict[str, str]]:
    payload = [
        {
            "clause_id": item.clause_id,
            "source_kind": item.source_kind,
            "text": item.source_text,
        }
        for item in clauses
    ]
    system = (
        "你是 JourneyPilot 请求合同规范化器。逐条处理输入 clause，不得遗漏、合并或新增 clause。"
        "仅把本次任务要求写成 Intent；目的地、日期、出发地等已经存在于 controlled identity 的事实标记为 controlled_identity。"
        "旅行安全、预算、交通、住宿和行动能力要求同时写入 constraints；数量、频率、主题、排除、"
        "顺序、输出字段和多方案要求属于 Intent。冲突和无法可靠理解的指令标记 unresolved，不得猜测。"
        "明确要求必须包含多个命名事项时，每个事项分别输出一个 hard MUST_INCLUDE Intent，不得合并成 objective。"
        "例如“必须安排甲地、乙地”要输出两个 Intent，两个 category value 分别只含甲地、乙地。"
        "预算金额只能来自含预算、费用、货币或上限语义的原文，并原样保留 amount、currency、per。"
        "只有约束、没有 Intent 的 clause 使用 mapped_to_constraint；同时含 Intent 和约束时使用 mapped_to_intent。"
        "明确要求备选方案时输出 hard ALTERNATIVES，target=delivery；未写数量表示主方案之外至少一个备选，count=2。"
        "source_kind 只用于确定优先级，不得改写。只输出符合 JSON Schema 的对象。"
    )
    user = json.dumps(
        {"controlled_identity": controlled_identity, "clauses": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def normalize_clauses(
    *,
    clauses: List[SourceClause],
    controlled_identity: Dict[str, object],
    llm: object,
) -> RequestContractNormalizationResult:
    if not clauses:
        raise ValueError("request normalization requires at least one clause")
    model_schema = RequestContractNormalizationResult.model_json_schema()
    capabilities = getattr(llm, "capabilities", None)
    supports_native_schema = bool(getattr(capabilities, "supports_json_schema", False))
    # 原生 Structured Output 要求所有字段 required；json_object 降级则把 Schema
    # 放进 prompt，此时保留 Pydantic 的可选/default 语义，避免模型把数十个可选字段
    # 全部展开成 null/[] 并撞上输出上限。两条路径最后都由同一 Pydantic 模型校验。
    schema = as_strict_schema(model_schema) if supports_native_schema else model_schema
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "request_contract_normalization",
            "strict": supports_native_schema,
            "schema": schema,
        },
    }
    messages = _normalization_prompt(clauses, controlled_identity)
    last_error: Exception | None = None
    loop = asyncio.get_running_loop()
    operation_deadline = loop.time() + INTENT_NORMALIZATION_OPERATION_TIMEOUT_SECONDS
    for attempt in range(3):
        raw = ""
        remaining_seconds = operation_deadline - loop.time()
        if remaining_seconds <= 0:
            last_error = TimeoutError(
                "request contract normalization operation budget exhausted"
            )
            break
        try:
            raw = await asyncio.wait_for(
                llm.ainvoke(
                    messages,
                    response_format=response_format,
                    temperature=0,
                    max_output_tokens=INTENT_NORMALIZATION_OUTPUT_TOKENS,
                ),
                timeout=min(
                    INTENT_NORMALIZATION_CALL_TIMEOUT_SECONDS,
                    remaining_seconds,
                ),
            )
            parsed = safe_parse_json(raw, strip_think_tags=True)
            result = RequestContractNormalizationResult.model_validate(parsed)
            _validate_clause_coverage(result, clauses)
            _validate_model_contract(result, clauses)
            return result
        except asyncio.TimeoutError as exc:
            last_error = exc
            remaining_seconds = operation_deadline - loop.time()
            if attempt < 2 and remaining_seconds > 0:
                logger.info(
                    "request contract model normalization timed out; requesting "
                    "fresh semantic regeneration | remaining_seconds=%.1f",
                    remaining_seconds,
                )
                messages = _normalization_fresh_retry_prompt(
                    clauses=clauses,
                    controlled_identity=controlled_identity,
                    error=TimeoutError(
                        "the previous semantic normalization exceeded its call budget"
                    ),
                )
                continue
            break
        except (
            OpenAIError,
            ValidationError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as exc:
            last_error = exc
            if attempt == 0:
                logger.info(
                    "request contract model normalization rejected; requesting semantic repair "
                    "| error_type=%s",
                    type(exc).__name__,
                )
                messages = _normalization_repair_prompt(
                    clauses=clauses,
                    controlled_identity=controlled_identity,
                    previous_output=raw,
                    error=exc,
                )
                continue
            if attempt == 1:
                logger.info(
                    "request contract semantic repair rejected; requesting fresh semantic "
                    "regeneration | error_type=%s",
                    type(exc).__name__,
                )
                messages = _normalization_fresh_retry_prompt(
                    clauses=clauses,
                    controlled_identity=controlled_identity,
                    error=exc,
                )
                continue
    logger.warning(
        "request contract model normalization failed after semantic repair; "
        "using deterministic fallback | error_type=%s",
        type(last_error).__name__ if last_error is not None else "unknown",
    )
    return _fallback_normalization(clauses, controlled_identity)


def _normalization_fresh_retry_prompt(
    *,
    clauses: List[SourceClause],
    controlled_identity: Dict[str, object],
    error: Exception,
) -> List[Dict[str, str]]:
    """Regenerate independently when editing the rejected draft stays anchored.

    The original clauses and schema remain authoritative.  Omitting the previous
    JSON is intentional: an otherwise capable model can repeat a structural
    mistake simply because the repair conversation presents that mistake as its
    latest assistant answer.
    """

    messages = _normalization_prompt(clauses, controlled_identity)
    messages.append(
        {
            "role": "user",
            "content": (
                "前两次结构化结果均未通过合同校验。请从原始 clauses 重新做一次独立的"
                "语义归一化，不要沿用或猜测之前的 JSON。每个 clause 仍须按原顺序恰好返回一次；"
                "复合要求要拆成各自可验收的 Intent。只输出完整 JSON 对象。"
                f"最近校验反馈：{str(error)[:1200]}"
            ),
        }
    )
    return messages


def _normalization_repair_prompt(
    *,
    clauses: List[SourceClause],
    controlled_identity: Dict[str, object],
    previous_output: str,
    error: Exception,
) -> List[Dict[str, str]]:
    """Ask the semantic authority to repair its own rejected structured draft."""

    messages = _normalization_prompt(clauses, controlled_identity)
    if previous_output.strip():
        messages.append(
            {
                "role": "assistant",
                "content": previous_output[-12_000:],
            }
        )
    messages.append(
        {
            "role": "user",
            "content": (
                "上一个结构化结果未通过请求合同校验。请重新理解全部原始 clauses，"
                "修复语义映射后输出一份完整的新 JSON 对象，不要解释、不要只输出差异。"
                f"校验错误类型：{type(error).__name__}；"
                f"校验反馈：{str(error)[:1200]}"
            ),
        }
    )
    return messages


def _validate_clause_coverage(
    result: RequestContractNormalizationResult, clauses: List[SourceClause]
) -> None:
    expected = [item.clause_id for item in clauses]
    actual = [item.clause_id for item in result.clauses]
    if actual != expected:
        raise ValueError("normalizer must return every clause once and in source order")


def _fallback_normalization(
    clauses: List[SourceClause], controlled_identity: Dict[str, object]
) -> RequestContractNormalizationResult:
    """Build the minimum deterministic contract after model failure only."""

    fallback = RequestContractNormalizationResult(
        clauses=[_fallback_clause(item, controlled_identity) for item in clauses]
    )
    return _enforce_deterministic_constraints(
        _enforce_deterministic_intents(fallback, clauses), clauses
    )


def _enforce_deterministic_constraints(
    result: RequestContractNormalizationResult,
    clauses: List[SourceClause],
) -> RequestContractNormalizationResult:
    """Restore numeric constraints in the deterministic fallback path.

    This function is never applied to a valid model result.  It only hardens the
    fallback drafts built by :func:`_fallback_normalization` after the model call,
    schema validation, or contract validation has failed.
    """

    normalized: List[NormalizedClauseDraft] = []
    for source, draft in zip(clauses, result.clauses):
        deterministic = deterministic_budget_constraints(source.source_text)
        non_budget = [
            item for item in draft.constraints if item.category != "budget_cap"
        ]
        if deterministic:
            constraints = [
                *non_budget,
                *[
                    NormalizedConstraintDraft.model_validate(item)
                    for item in deterministic
                ],
            ]
        else:
            source_text = source.source_text.casefold()
            constraints = [
                *non_budget,
                *[
                    item
                    for item in draft.constraints
                    if item.category == "budget_cap"
                    and (
                        item.params.amount is None
                        or any(cue in source_text for cue in _MONETARY_CUES)
                    )
                ],
            ]
        if constraints == draft.constraints:
            normalized.append(draft)
            continue
        disposition = draft.disposition
        reason_code = draft.reason_code
        if constraints and not draft.intents:
            disposition = ClauseDisposition.MAPPED_TO_CONSTRAINT
            reason_code = None
        normalized.append(
            draft.model_copy(
                update={
                    "constraints": constraints,
                    "disposition": disposition,
                    "reason_code": reason_code,
                }
            )
        )
    return RequestContractNormalizationResult(clauses=normalized)


_EXPLICIT_MUST_INCLUDE = re.compile(
    r"^(?:请)?(?:务必|必须|一定要)(?:在行程中)?"
    r"(?:安排|游览|参观|去|打卡)\s*(?P<values>.+)$",
    re.IGNORECASE,
)
_EXPLICIT_ITEM_SEPARATOR = re.compile(r"[、,，]+")
_MONETARY_CUES = (
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
    "budget",
)
_ALTERNATIVE_REQUEST_CUES = (
    "备选方案",
    "备用方案",
    "替代方案",
    "alternative option",
    "backup option",
)


def _explicit_must_include_terms(text: str) -> list[str]:
    """Return independently enforceable items from an explicit include clause.

    ``、`` and commas are unambiguous list separators in this contract.  Plain
    Chinese ``和`` is deliberately not split because it is also part of proper
    names such as ``颐和园``.
    """

    match = _EXPLICIT_MUST_INCLUDE.match(text.strip())
    if match is None:
        return []
    terms = [
        item.strip(" \t。；;！!？?")
        for item in _EXPLICIT_ITEM_SEPARATOR.split(match.group("values"))
    ]
    terms = [item for item in terms if item and len(item) <= 80]
    return list(dict.fromkeys(terms))


def _validate_model_contract(
    result: RequestContractNormalizationResult,
    clauses: List[SourceClause],
) -> None:
    """Reject a structurally valid model answer that violates source invariants.

    This validator never authors or rewrites an intent.  Semantic normalization
    belongs to the model; these checks only decide whether its structured answer
    is safe to accept.  A rejected result enters the isolated deterministic
    fallback path as a whole, so model and rule drafts are never mixed.
    """

    for source, draft in zip(clauses, result.clauses):
        if is_material_clause(source.source_text) and draft.disposition in {
            ClauseDisposition.BACKGROUND_CONTEXT,
            ClauseDisposition.NON_ACTIONABLE,
        }:
            raise ValueError("model dropped a material request clause")

        terms = _explicit_must_include_terms(source.source_text)
        if terms:
            available = {
                index: intent
                for index, intent in enumerate(draft.intents)
                if intent.kind is IntentKind.MUST_INCLUDE
                and intent.strength is IntentStrength.HARD
                and isinstance(intent.value, CategoryIntentValue)
            }
            for term in terms:
                normalized_term = term.casefold()
                match_index = next(
                    (
                        index
                        for index, intent in available.items()
                        if any(
                            normalized_term in category.casefold()
                            or category.casefold() in normalized_term
                            for category in intent.value.categories
                        )
                    ),
                    None,
                )
                if match_index is None:
                    raise ValueError(
                        "model did not emit one hard must-include intent per required item"
                    )
                available.pop(match_index)

        deterministic_budget = deterministic_budget_constraints(source.source_text)
        if deterministic_budget:
            expected = deterministic_budget[0]["params"]
            if not any(
                constraint.category == "budget_cap"
                and constraint.params.amount == expected["amount"]
                and str(constraint.params.currency or "").upper()
                == str(expected["currency"]).upper()
                and constraint.params.per == expected["per"]
                for constraint in draft.constraints
            ):
                raise ValueError(
                    "model changed or omitted an explicit numeric budget cap"
                )

        source_text = source.source_text.casefold()
        if not any(cue in source_text for cue in _MONETARY_CUES) and any(
            constraint.category == "budget_cap" and constraint.params.amount is not None
            for constraint in draft.constraints
        ):
            raise ValueError("model inferred a numeric budget from non-monetary text")

        if any(cue in source_text for cue in _ALTERNATIVE_REQUEST_CUES) and not any(
            intent.kind is IntentKind.ALTERNATIVES
            and intent.strength is IntentStrength.HARD
            and intent.target is IntentTarget.DELIVERY
            and isinstance(intent.value, AlternativeIntentValue)
            for intent in draft.intents
        ):
            raise ValueError("model omitted an explicit alternative request")


def _enforce_deterministic_intents(
    result: RequestContractNormalizationResult,
    clauses: List[SourceClause],
) -> RequestContractNormalizationResult:
    """Split explicit named must-dos in the deterministic fallback path.

    A valid model result is returned unchanged.  This grammar is used only after
    model/schema/contract failure so the degraded run still gives each listed
    item its own stable ID, candidate match, selection role, and fidelity verdict.
    """

    normalized: List[NormalizedClauseDraft] = []
    for source, draft in zip(clauses, result.clauses):
        terms = _explicit_must_include_terms(source.source_text)
        if not terms:
            normalized.append(draft)
            continue
        preserved = [
            intent
            for intent in draft.intents
            if intent.kind not in {IntentKind.OBJECTIVE, IntentKind.MUST_INCLUDE}
        ]
        explicit = [
            _intent(
                IntentKind.MUST_INCLUDE,
                _target_for_text(term),
                IntentStrength.HARD,
                CategoryIntentValue(categories=[term]),
                f"必须安排{term}",
                ["research", "admission", "ranking", "composition"],
                VerificationMode.MIXED,
            )
            for term in terms
        ]
        normalized.append(
            draft.model_copy(
                update={
                    "disposition": ClauseDisposition.MAPPED_TO_INTENT,
                    "reason_code": None,
                    "intents": _dedupe_drafts([*preserved, *explicit]),
                }
            )
        )
    return RequestContractNormalizationResult(clauses=normalized)


def _fallback_clause(
    clause: SourceClause, controlled_identity: Dict[str, object]
) -> NormalizedClauseDraft:
    text = clause.source_text
    lowered = text.lower()
    identity_tokens = _identity_tokens(controlled_identity)
    mentions_identity = any(token and token in text for token in identity_tokens)

    # 一个 clause 同时跨多个业务领域时，规则层没有资格猜它们之间是并列、条件还是
    # 作用域关系。语义模型两次失败后的安全兜底是明确留下 unresolved，而不是把
    # “高铁、住宿、每天时间和交通”压成单个 Cadence/Objective，造成下游假满足。
    if len(_fallback_explicit_domains(text)) > 1:
        return NormalizedClauseDraft(
            clause_id=clause.clause_id,
            disposition=ClauseDisposition.UNRESOLVED,
            reason_code="semantic_normalization_required",
        )

    intents: List[IntentDraft] = []
    if match := re.search(
        r"(?:每天|每日).{0,10}(?:最多|不超过)\s*(\d+)\s*(?:个|处|家)", text
    ):
        intents.append(
            _intent(
                IntentKind.QUANTITY,
                _target_for_text(text),
                IntentStrength.HARD,
                CountIntentValue(
                    operator="at_most", count=int(match.group(1)), unit="day"
                ),
                "每天主要安排数量受上限约束",
                ["composition"],
                VerificationMode.DETERMINISTIC,
            )
        )
    elif match := re.search(
        r"(?:每天|每日).{0,10}(?:至少)\s*(\d+)\s*(?:个|处|家)", text
    ):
        intents.append(
            _intent(
                IntentKind.QUANTITY,
                _target_for_text(text),
                IntentStrength.HARD,
                CountIntentValue(
                    operator="at_least", count=int(match.group(1)), unit="day"
                ),
                "每天主要安排数量有最低要求",
                ["research", "composition"],
                VerificationMode.DETERMINISTIC,
            )
        )
    if "每天" in text or "每日" in text:
        intents.append(
            _intent(
                IntentKind.CADENCE,
                _target_for_text(text),
                IntentStrength.HARD,
                CadenceIntentValue(
                    frequency="once_per_day",
                    time_window=_time_window(text),
                    required_attributes=_attributes(text),
                ),
                text,
                ["research", "composition"],
                VerificationMode.MIXED,
            )
        )
    if any(
        token in lowered for token in ("不要", "不去", "避开", "禁止", "avoid", "no ")
    ):
        intents.append(
            _intent(
                IntentKind.MUST_EXCLUDE,
                _target_for_text(text),
                IntentStrength.HARD,
                CategoryIntentValue(categories=[text]),
                text,
                ["research", "admission", "composition"],
                VerificationMode.MIXED,
            )
        )
    if any(token in text for token in ("重点", "主题", "摄影", "建筑", "文化")):
        intents.append(
            _intent(
                IntentKind.THEME,
                IntentTarget.VISIT,
                IntentStrength.SOFT,
                CategoryIntentValue(categories=[text]),
                text,
                ["research", "ranking", "composition"],
                VerificationMode.SEMANTIC,
            )
        )
    if any(token in text for token in ("解释", "说明", "列出", "标注")):
        intents.append(
            _intent(
                IntentKind.OUTPUT_REQUIREMENT,
                _target_for_text(text),
                IntentStrength.HARD,
                OutputRequirementValue(required_field=text, applies_to="each_item"),
                text,
                ["projection"],
                VerificationMode.MIXED,
            )
        )
    if any(
        token in lowered
        for token in (
            "再来一套",
            "换一套",
            "换一批",
            "不同风格",
            "更小众",
            "小众一点",
            "another version",
            "different set",
            "more niche",
        )
    ):
        intents.append(
            _intent(
                IntentKind.DIVERSITY,
                IntentTarget.VISIT,
                IntentStrength.SOFT,
                ScalarIntentValue(value=text),
                "探索另一组同等合规的候选",
                ["research", "ranking", "composition"],
                VerificationMode.DETERMINISTIC,
            )
        )
    if match := re.search(r"([二两三四五六七八九十\d]+)\s*套.{0,12}方案", text):
        count = _chinese_number(match.group(1))
        if count >= 2:
            intents.append(
                _intent(
                    IntentKind.ALTERNATIVES,
                    IntentTarget.DELIVERY,
                    IntentStrength.HARD,
                    AlternativeIntentValue(count=count),
                    text,
                    ["composition", "projection"],
                    VerificationMode.DETERMINISTIC,
                )
            )
    elif any(token in lowered for token in _ALTERNATIVE_REQUEST_CUES):
        # A singular request for an alternative means one primary plus one
        # fallback option.  Unlike the numbered grammar above there is no count
        # to infer, so two is the smallest contract that satisfies the request.
        intents.append(
            _intent(
                IntentKind.ALTERNATIVES,
                IntentTarget.DELIVERY,
                IntentStrength.HARD,
                AlternativeIntentValue(count=2),
                text,
                ["composition", "projection"],
                VerificationMode.DETERMINISTIC,
            )
        )
    if not intents and any(token in text for token in ("规划", "行程", "安排")):
        intents.append(
            _intent(
                IntentKind.OBJECTIVE,
                IntentTarget.TRIP,
                IntentStrength.HARD,
                ScalarIntentValue(value=text),
                text,
                ["research", "composition", "projection"],
                VerificationMode.MIXED,
            )
        )
    if intents:
        return NormalizedClauseDraft(
            clause_id=clause.clause_id,
            disposition=ClauseDisposition.MAPPED_TO_INTENT,
            intents=_dedupe_drafts(intents),
            constraints=[],
        )
    if mentions_identity:
        return NormalizedClauseDraft(
            clause_id=clause.clause_id,
            disposition=ClauseDisposition.CONTROLLED_IDENTITY,
        )
    if is_material_clause(text):
        return NormalizedClauseDraft(
            clause_id=clause.clause_id,
            disposition=ClauseDisposition.UNRESOLVED,
            reason_code="normalization_failed",
        )
    return NormalizedClauseDraft(
        clause_id=clause.clause_id,
        disposition=ClauseDisposition.BACKGROUND_CONTEXT,
    )


def _intent(
    kind: IntentKind,
    target: IntentTarget,
    strength: IntentStrength,
    value: IntentValue,
    summary: str,
    impact_stages: List[IntentImpactStage],
    verification: VerificationMode,
) -> IntentDraft:
    return IntentDraft(
        kind=kind,
        target=target,
        strength=strength,
        priority=90 if strength is IntentStrength.HARD else 60,
        value=value,
        verification_mode=verification,
        impact_stages=impact_stages,
        public_summary=summary[:300],
    )


def _identity_tokens(identity: Dict[str, object]) -> List[str]:
    tokens: List[str] = []
    origin = identity.get("origin")
    if isinstance(origin, dict):
        tokens.extend(str(origin.get(key) or "") for key in ("name", "display_name"))
    for destination in identity.get("destinations") or []:
        if isinstance(destination, dict):
            tokens.extend(
                str(destination.get(key) or "") for key in ("name", "display_name")
            )
    tokens.extend(str(identity.get(key) or "") for key in ("start_date", "end_date"))
    return [token for token in tokens if token]


def _target_for_text(text: str) -> IntentTarget:
    if any(token in text for token in ("咖啡", "餐", "美食", "早餐", "午餐", "晚餐")):
        return IntentTarget.DINING
    if any(token in text for token in ("酒店", "住宿", "房间", "民宿")):
        return IntentTarget.LODGING
    if any(
        token in text
        for token in ("高铁", "火车", "航班", "飞机", "去程", "返程", "往返")
    ):
        return IntentTarget.LONG_DISTANCE_TRANSPORT
    if any(token in text for token in ("交通", "地铁", "公交", "步行", "打车")):
        return IntentTarget.LOCAL_TRANSPORT
    return IntentTarget.VISIT


def _fallback_explicit_domains(text: str) -> set[IntentTarget]:
    """Identify only explicit domain nouns for conservative fallback safety."""

    domains: set[IntentTarget] = set()
    if any(token in text for token in ("咖啡", "餐", "美食", "早餐", "午餐", "晚餐")):
        domains.add(IntentTarget.DINING)
    if any(token in text for token in ("酒店", "住宿", "房间", "民宿")):
        domains.add(IntentTarget.LODGING)
    if any(
        token in text
        for token in ("高铁", "火车", "航班", "飞机", "去程", "返程", "往返")
    ):
        domains.add(IntentTarget.LONG_DISTANCE_TRANSPORT)
    if any(token in text for token in ("市内交通", "地铁", "公交", "步行", "打车")):
        domains.add(IntentTarget.LOCAL_TRANSPORT)
    return domains


def _time_window(text: str) -> Optional[str]:
    for value in (
        "上午",
        "中午",
        "下午",
        "傍晚",
        "晚上",
        "morning",
        "afternoon",
        "evening",
    ):
        if value in text.lower():
            return value
    return None


def _attributes(text: str) -> List[str]:
    return [
        value for value in ("安静", "无障碍", "亲子", "本地", "室内") if value in text
    ]


def _chinese_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    return {
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }.get(value, 0)


def _dedupe_drafts(items: List[IntentDraft]) -> List[IntentDraft]:
    unique: Dict[str, IntentDraft] = {}
    for item in items:
        key = json.dumps(
            item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
        )
        unique.setdefault(key, item)
    return list(unique.values())

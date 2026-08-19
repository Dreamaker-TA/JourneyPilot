from __future__ import annotations

from enum import Enum
from typing import Dict, List, Literal

from pydantic import Field, model_validator

from .contract_base import StrictModel
from .research_domain import ResearchDomain


class ResearchQueryKind(str, Enum):
    INTENT_PRIMARY = "intent_primary"
    STRUCTURAL = "structural"
    EVIDENCE_ENRICHMENT = "evidence_enrichment"
    GENERIC_FALLBACK = "generic_fallback"
    TARGETED_REPAIR = "targeted_repair"


class ResearchQuery(StrictModel):
    query_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    domain: ResearchDomain
    destination_id: str = Field(min_length=1)
    query_kind: ResearchQueryKind
    query_text: str = Field(min_length=1, max_length=500)
    aliases: List[str] = Field(default_factory=list)
    intent_ids: List[str] = Field(default_factory=list)
    excluded_categories: List[str] = Field(default_factory=list)
    desired_candidate_count: int = Field(ge=1, le=20)
    provider_route: Literal[
        "rag",
        "web_discovery",
        "global_place_search",
        "amap_place_search",
        "rail_provider",
        "route_provider",
        "mixed",
    ]
    priority: int = Field(ge=0, le=100)
    fallback_after_query_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_query(self) -> "ResearchQuery":
        if len(self.aliases) != len(set(self.aliases)):
            raise ValueError("research query aliases must be unique")
        if len(self.intent_ids) != len(set(self.intent_ids)):
            raise ValueError("research query intent ids must be unique")
        if (
            self.query_kind
            in {ResearchQueryKind.INTENT_PRIMARY, ResearchQueryKind.TARGETED_REPAIR}
            and not self.intent_ids
        ):
            raise ValueError("intent-scoped research query requires an intent id")
        if len(self.fallback_after_query_ids) != len(
            set(self.fallback_after_query_ids)
        ):
            raise ValueError("research query fallback dependencies must be unique")
        if (
            self.query_kind is ResearchQueryKind.GENERIC_FALLBACK
            and not self.fallback_after_query_ids
        ):
            raise ValueError("generic fallback requires prior query dependencies")
        return self


class ResearchQueryPlan(StrictModel):
    schema_version: Literal["journeypilot.research_query_plan.v1"] = (
        "journeypilot.research_query_plan.v1"
    )
    query_plan_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    intent_spec_revision: int = Field(ge=1)
    queries: List[ResearchQuery] = Field(default_factory=list)
    per_domain_candidate_caps: Dict[str, int] = Field(default_factory=dict)
    policy_version: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> "ResearchQueryPlan":
        query_ids = [query.query_id for query in self.queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("research query ids must be unique")
        known_ids = set(query_ids)
        if any(query.generation_id != self.generation_id for query in self.queries):
            raise ValueError("research query belongs to another generation")
        if any(
            not set(query.fallback_after_query_ids) <= known_ids
            for query in self.queries
        ):
            raise ValueError("research query references an unknown fallback dependency")
        expected_domains = {domain.value for domain in ResearchDomain}
        if set(self.per_domain_candidate_caps) != expected_domains:
            raise ValueError("research query plan requires one cap for every domain")
        if any(
            isinstance(value, bool) or value < 1 or value > 100
            for value in self.per_domain_candidate_caps.values()
        ):
            raise ValueError("research query candidate caps must be between 1 and 100")
        index = self.query_index()
        for query in self.queries:
            if query.query_kind is not ResearchQueryKind.GENERIC_FALLBACK:
                continue
            if any(
                index[query_id].destination_id != query.destination_id
                or index[query_id].domain is not query.domain
                or index[query_id].query_kind is ResearchQueryKind.GENERIC_FALLBACK
                for query_id in query.fallback_after_query_ids
            ):
                raise ValueError(
                    "generic fallback dependencies must be prior domain queries"
                )
        return self

    def query_index(self) -> Dict[str, ResearchQuery]:
        return {query.query_id: query for query in self.queries}

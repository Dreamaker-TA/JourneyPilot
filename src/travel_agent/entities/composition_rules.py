from __future__ import annotations

from enum import Enum
from typing import Annotated, List, Literal, Optional, Union

from pydantic import Field, model_validator

from .contract_base import StrictModel
from .research_domain import ResearchDomain


COMPOSITION_RULE_POLICY_VERSION = "composition_rules.v1"
COMPOSITION_PROMPT_VERSION = "itinerary_composition.v1"


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


class PlacementRuleParameters(StrictModel):
    parameter_kind: Literal["placement"] = "placement"
    values: List[str] = Field(min_length=1)


class CountRuleParameters(StrictModel):
    parameter_kind: Literal["count"] = "count"
    count: int = Field(ge=0)
    unit: Literal["trip", "day", "destination"]


class CadenceRuleParameters(StrictModel):
    parameter_kind: Literal["cadence"] = "cadence"
    frequency: Literal[
        "once_per_trip",
        "once_per_destination",
        "once_per_day",
        "selected_days",
    ]
    count: int = Field(ge=1)
    time_window: Optional[str] = Field(default=None, max_length=80)
    required_attributes: List[str] = Field(default_factory=list)


class TimeWindowRuleParameters(StrictModel):
    parameter_kind: Literal["time_window"] = "time_window"
    window: str = Field(min_length=1, max_length=80)
    applies_to: Optional[str] = Field(default=None, max_length=120)


class SequenceRuleParameters(StrictModel):
    parameter_kind: Literal["sequence"] = "sequence"
    ordered_items: List[str] = Field(min_length=2)


class DestinationScopeRuleParameters(StrictModel):
    parameter_kind: Literal["destination_scope"] = "destination_scope"
    area: str = Field(min_length=1, max_length=160)
    relation: Literal["inside", "near", "avoid", "same_area"]


class RestWindowRuleParameters(StrictModel):
    parameter_kind: Literal["rest_window"] = "rest_window"
    window: str = Field(min_length=1, max_length=80)


class MaxTravelTimeRuleParameters(StrictModel):
    parameter_kind: Literal["max_travel_time"] = "max_travel_time"
    minutes: int = Field(ge=0)


class OutputExplanationRuleParameters(StrictModel):
    parameter_kind: Literal["output_explanation"] = "output_explanation"
    required_field: str = Field(min_length=1, max_length=160)
    applies_to: Literal["each_item", "each_day", "trip", "delivery"]


CompositionRuleParameters = Annotated[
    Union[
        PlacementRuleParameters,
        CountRuleParameters,
        CadenceRuleParameters,
        TimeWindowRuleParameters,
        SequenceRuleParameters,
        DestinationScopeRuleParameters,
        RestWindowRuleParameters,
        MaxTravelTimeRuleParameters,
        OutputExplanationRuleParameters,
    ],
    Field(discriminator="parameter_kind"),
]


class CompositionRule(StrictModel):
    rule_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    rule_kind: CompositionRuleKind
    target_domain: Optional[ResearchDomain]
    hard: bool
    policy_on_failure: Literal[
        "never_violate",
        "repair_then_deviate",
        "nonblocking_preference",
    ]
    parameters: CompositionRuleParameters

    @model_validator(mode="after")
    def validate_kind_parameters(self) -> "CompositionRule":
        expected = {
            CompositionRuleKind.MUST_PLACE: "placement",
            CompositionRuleKind.MUST_NOT_PLACE: "placement",
            CompositionRuleKind.MAX_PER_DAY: "count",
            CompositionRuleKind.MIN_PER_DAY: "count",
            CompositionRuleKind.CADENCE: "cadence",
            CompositionRuleKind.TIME_WINDOW: "time_window",
            CompositionRuleKind.SEQUENCE: "sequence",
            CompositionRuleKind.DESTINATION_SCOPE: "destination_scope",
            CompositionRuleKind.REST_WINDOW: "rest_window",
            CompositionRuleKind.MAX_TRAVEL_TIME: "max_travel_time",
            CompositionRuleKind.OUTPUT_EXPLANATION: "output_explanation",
        }[self.rule_kind]
        if self.parameters.parameter_kind != expected:
            raise ValueError("composition rule parameters do not match rule kind")
        if self.policy_on_failure == "never_violate" and not self.hard:
            raise ValueError("only hard composition rules may be never-violate")
        return self

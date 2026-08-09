"""Seed and database access for validated product configuration."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from ..infrastructure.database import get_db_session


class PlannerOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    inference_keywords: list[str] = Field(default_factory=list)
    is_default: bool = False


class TripPlannerConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_styles: list[PlannerOption] = Field(min_length=1)
    secondary_interests: list[PlannerOption] = Field(default_factory=list)
    default_adults: int = Field(ge=1, le=20)
    default_children: int = Field(ge=0, le=20)
    default_elderly_companions: bool
    default_accessibility_required: bool
    max_secondary_interests: int = Field(ge=0, le=10)
    inspiration_rotation_ms: int = Field(ge=3000, le=30000)
    inspiration_prompts: list[str] = Field(min_length=3)

    @model_validator(mode="after")
    def validate_defaults(self) -> "TripPlannerConfiguration":
        if sum(option.is_default for option in self.primary_styles) != 1:
            raise ValueError("primary_styles must contain exactly one default")
        if len({option.id for option in self.primary_styles}) != len(self.primary_styles):
            raise ValueError("primary style ids must be unique")
        if len({option.id for option in self.secondary_interests}) != len(self.secondary_interests):
            raise ValueError("secondary interest ids must be unique")
        if any(not prompt.strip() for prompt in self.inspiration_prompts):
            raise ValueError("inspiration prompts cannot be blank")
        return self


TRIP_PLANNER_CONFIG_KEY = "trip_planner"


TRIP_PLANNER_SEED = TripPlannerConfiguration(
    primary_styles=[
        PlannerOption(id="balanced", label="经典均衡", is_default=True),
        PlannerOption(id="slow", label="轻松慢游", inference_keywords=["轻松", "不太累", "慢"]),
        PlannerOption(id="food", label="当地美食", inference_keywords=["美食", "吃"]),
        PlannerOption(id="culture", label="人文体验", inference_keywords=["人文", "历史", "博物馆"]),
        PlannerOption(id="outdoors", label="户外自然", inference_keywords=["户外", "自然", "徒步"]),
        PlannerOption(id="family", label="亲子友好", inference_keywords=["亲子", "孩子", "小孩", "宝宝"]),
    ],
    secondary_interests=[
        PlannerOption(id="hidden_gems", label="小众探索"),
        PlannerOption(id="shopping", label="购物"),
        PlannerOption(id="nightlife", label="夜生活"),
        PlannerOption(id="photography", label="摄影"),
        PlannerOption(id="stays", label="住宿体验"),
        PlannerOption(id="public_transport", label="公共交通"),
        PlannerOption(id="local_food", label="在地饮食"),
        PlannerOption(id="slow_pace", label="慢节奏"),
    ],
    default_adults=1,
    default_children=0,
    default_elderly_companions=False,
    default_accessibility_required=False,
    max_secondary_interests=2,
    inspiration_rotation_ms=6500,
    inspiration_prompts=[
        "秋天想带爸妈去一个不太累、吃得好的地方，大约 6 天",
        "想找一个适合第一次独自旅行的海边城市，预算 5000 元",
        "计划和孩子看动物、坐火车，行程不要太赶",
        "想用一周体验当地市场、博物馆和社区生活",
        "我们喜欢徒步和摄影，希望每天保留充足的日落时间",
    ],
)


def validated_trip_planner_seed() -> TripPlannerConfiguration:
    """Return the release seed after validating the current product contract."""
    return TripPlannerConfiguration.model_validate(TRIP_PLANNER_SEED.model_dump(mode="json"))


class ProductConfigurationStore:
    async def ensure_seed(self) -> None:
        """Bootstrap a missing configuration without changing an existing environment."""
        payload = validated_trip_planner_seed().model_dump(mode="json")
        async with get_db_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO product_configurations (config_key, config, created_at, updated_at)
                    VALUES (:config_key, CAST(:config AS jsonb), NOW(), NOW())
                    ON CONFLICT (config_key) DO NOTHING
                    """
                ),
                {
                    "config_key": TRIP_PLANNER_CONFIG_KEY,
                    "config": json.dumps(payload, ensure_ascii=False),
                },
            )

    async def publish_seed(self) -> None:
        """Explicitly publish the reviewed seed to an existing environment.

        This method is deliberately separate from ``ensure_seed``: applications
        may bootstrap an empty database at startup, but only the release command
        is allowed to replace existing product data.
        """
        payload = validated_trip_planner_seed().model_dump(mode="json")
        async with get_db_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO product_configurations (config_key, config, created_at, updated_at)
                    VALUES (:config_key, CAST(:config AS jsonb), NOW(), NOW())
                    ON CONFLICT (config_key) DO UPDATE
                    SET config = EXCLUDED.config, updated_at = NOW()
                    """
                ),
                {
                    "config_key": TRIP_PLANNER_CONFIG_KEY,
                    "config": json.dumps(payload, ensure_ascii=False),
                },
            )

    async def get_trip_planner(self) -> TripPlannerConfiguration:
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT config FROM product_configurations WHERE config_key = :config_key"),
                {"config_key": TRIP_PLANNER_CONFIG_KEY},
            )
            row = result.mappings().first()
        if row is None:
            raise LookupError("trip_planner product configuration is not seeded")
        raw: Any = row["config"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        return TripPlannerConfiguration.model_validate(raw)

"""Explicit, release-only publication of system product configuration."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from ..infrastructure.database import init_db
from .product_config import (
    TRIP_PLANNER_CONFIG_KEY,
    ProductConfigurationStore,
    validated_trip_planner_seed,
)
from .store import PresetStore, validated_system_preset_seeds


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_publication_summary() -> Dict[str, Any]:
    """Validate release data and return a stable, reviewable publication plan."""
    trip_planner = validated_trip_planner_seed().model_dump(mode="json")
    system_presets = [
        preset.model_dump(mode="json", exclude_unset=True)
        for preset in validated_system_preset_seeds()
    ]
    return {
        "trip_planner": {
            "config_key": TRIP_PLANNER_CONFIG_KEY,
            "digest": _digest(trip_planner),
        },
        "system_presets": {
            "count": len(system_presets),
            "ids": [preset["id"] for preset in system_presets],
            "digest": _digest(system_presets),
        },
    }


async def publish_seed() -> Dict[str, Any]:
    """Validate and explicitly replace release-managed product data in the DB."""
    summary = build_publication_summary()
    await init_db()
    await ProductConfigurationStore().publish_seed()
    await PresetStore().publish_system_presets()
    return summary

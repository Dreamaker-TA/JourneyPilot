"""Read-only, database-backed product configuration endpoints."""

from fastapi import APIRouter, HTTPException

from ...preset.product_config import ProductConfigurationStore, TripPlannerConfiguration

router = APIRouter(prefix="/api/product", tags=["product"])
_store = ProductConfigurationStore()


@router.get("/trip-planner", response_model=TripPlannerConfiguration)
async def get_trip_planner_configuration() -> TripPlannerConfiguration:
    try:
        return await _store.get_trip_planner()
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

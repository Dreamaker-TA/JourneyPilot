"""Planning-time destination geography derived from controlled trip identity."""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import Field

from .delivery_bundle import StrictModel


class DestinationGeoPoint(StrictModel):
    destination_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    country_code: str = Field(min_length=2, max_length=2)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    trip_start_date: date
    trip_end_date: date
    timezone: Optional[str] = Field(default=None, min_length=1)

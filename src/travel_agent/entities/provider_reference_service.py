"""A real provider service the Run could not confirm for the traveller's date.

12306 sells a rolling pre-sale window (``tools.temporal.RAIL_LIVE_INVENTORY_DAYS``),
so a return leg dated beyond it cannot be queried for the date the traveller is
actually travelling.  The Gateway's date-capability decision therefore queries the
window's edge instead and keeps what came back: a real train, with a real number,
real times and a real observed fare — for a *different* day.

That data has no evidence standing and must never acquire any.  It is not
compiled into a SourceRecord, it supports no FactAssertion, no candidate is built
from it, and the envelope it came from keeps ``evidence_allowed=False``.  What it
gets instead is a sentence: the traveller is told the concrete service that
exists on the route, told which of its claims were not confirmed for their date,
and told to re-check before travelling.  A plan that says that is more use than
one that says nothing, and it is honest in a way an itinerary leg built from the
same data could not be — a leg would need admitted facts for a date on which
nothing was admitted.

This module owns both the record and its wording.  The note is composed once, at
Workspace materialization, and read verbatim by the report, the PDF and the
public payload — none of them re-derives it.
"""

from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderReferenceService(BaseModel):
    """One provider service observed off-date, with the claims that stay unconfirmed.

    ``unconfirmed_claims`` is copied verbatim from the provider envelope's
    ``claims_not_confirmed_for_requested_date``.  It is never re-derived here:
    which claims a supplier declines to carry across dates is the supplier's
    statement, and a hardcoded list here would keep asserting the old answer
    after the supplier changed it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    leg_role: Literal["outbound", "return"]
    from_name: str = Field(min_length=1)
    to_name: str = Field(min_length=1)
    service_number: str = Field(min_length=1)
    departure_time: str = Field(min_length=1)
    arrival_time: str = Field(min_length=1)
    duration_minutes: Optional[int] = Field(default=None, ge=0)
    lowest_observed_price_cny: Optional[float] = Field(default=None, ge=0)
    requested_date: date
    reference_date: date
    unconfirmed_claims: List[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dates(self) -> "ProviderReferenceService":
        if self.reference_date == self.requested_date:
            raise ValueError(
                "a reference service is only a reference for another date"
            )
        return self


_LEG_ROLE_LABELS = {"outbound": "去程", "return": "返程"}

# The provider's own claim keys, in the order it lists them.  An unknown key is
# printed as it arrived rather than dropped: a claim nobody has a label for is
# still a claim the traveller was told was unconfirmed.
_CLAIM_LABELS = {
    "train_no": "车次",
    "departure_time": "出发时刻",
    "arrival_time": "到达时刻",
    "price": "票价",
    "inventory": "余票",
}


def _duration_clause(duration_minutes: Optional[int]) -> str:
    if not duration_minutes:
        return ""
    hours, minutes = divmod(int(duration_minutes), 60)
    if hours and minutes:
        return f"，约 {hours} 小时 {minutes} 分"
    if hours:
        return f"，约 {hours} 小时"
    return f"，约 {minutes} 分钟"


def _price_clause(price: Optional[float]) -> str:
    if price is None:
        return ""
    amount = int(price) if float(price).is_integer() else price
    return f"，最低票价 ¥{amount}"


def reference_service_note(service: ProviderReferenceService) -> str:
    """The one sentence every delivery surface prints for a reference service."""

    claims = "、".join(
        _CLAIM_LABELS.get(claim, claim) for claim in service.unconfirmed_claims
    )
    return (
        f"{_LEG_ROLE_LABELS[service.leg_role]}参考班次 {service.service_number}："
        f"{service.from_name} {service.departure_time} → "
        f"{service.to_name} {service.arrival_time}"
        f"{_duration_clause(service.duration_minutes)}"
        f"{_price_clause(service.lowest_observed_price_cny)}；"
        f"这是供应商在 {service.reference_date.isoformat()} 的真实班次，"
        f"未对 {service.requested_date.isoformat()} 确认的是{claims}，"
        "出行前请按实际出行日期重新核对。"
    )

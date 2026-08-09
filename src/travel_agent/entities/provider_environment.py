"""Which of a supplier's environments the evidence behind a leg came from.

A supplier's sandbox returns well-formed, plausible, *fictional* services.  On the
exported PDF a sandbox flight and a bookable one were indistinguishable: same
carrier name, same times, same price, no marking of any kind.  The one field that
knew — Duffel's ``live_mode`` — was dropped twice on the way out, so the only
honest option left to the interactive card was hardcoded copy, which would turn
into a permanent lie in an exported document the day a live key was installed.

This module is the single reader of that fact.  It takes the value from the
provider's own sanitized payload rather than from configuration, because
configuration says what key we *think* is installed while the payload says which
environment actually answered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .delivery_bundle import DeliveryBundle


DataEnvironment = Literal["production", "sandbox"]

# The one wording for this disclosure, read by the interactive card, the formal
# report and the exported PDF.  It says what is true of the evidence — "a sandbox
# answered" — rather than naming a supplier: the supplier is not at fault, and a
# name here would age badly the moment the mix of providers changes.
SANDBOX_EVIDENCE_NOTE = (
    "本方案中部分交通班次的依据来自供应商的开发（沙箱）环境，"
    "班次、时刻与价格仅供规划参考，不可直接预订。"
)

_VALID: frozenset[str] = frozenset({"production", "sandbox"})

# Suppliers we call that have no separate test environment: their responses are
# always the real world's.  Naming them is not a fallback — it is the answer.
_ALWAYS_PRODUCTION_PROVIDERS = frozenset(
    {
        "transitous",
        "nominatim",
        "amap",
        "12306",
        "open-meteo",
    }
)


def snapshot_data_environment(
    snapshot: Optional[Mapping[str, Any]],
    *,
    provider_name: str,
) -> DataEnvironment:
    """Read the environment out of one provider snapshot.

    ``snapshot`` is a ``SourceRecord.snapshot``, which has two shapes depending on
    whether the record came through the snapshot cache: the sanitized provider
    payload itself, or a Tool Envelope wrapping it under ``sanitized_result``.
    Both are read here so a cache hit and a live call cannot disagree about the
    environment of the same response.

    A provider that declares its environment is believed.  A provider with no test
    environment is production by identity.  Anything else raises: an unlabelled
    response from a supplier that *does* have a sandbox is exactly the case this
    field exists for, and guessing "production" there is the original defect.
    """

    declared = _declared(snapshot)
    if declared is not None:
        return declared  # type: ignore[return-value]
    if provider_name.strip().lower() in _ALWAYS_PRODUCTION_PROVIDERS:
        return "production"
    raise ValueError(
        f"provider {provider_name!r} did not state which data environment answered"
    )


@dataclass(frozen=True)
class ProviderEnvironmentView:
    """One Run's data-environment fact, reduced once and read by every surface.

    Run-level rather than per-leg on purpose.  A traveller does not act
    differently on leg three than on leg one here: the useful statement is "some
    of the transport evidence in this plan came from a test environment, do not
    try to book from it", and a per-leg badge would spend the reader's attention
    on a distinction that changes nothing they do.
    """

    has_sandbox_evidence: bool

    @classmethod
    def from_bundle(cls, bundle: "DeliveryBundle") -> "ProviderEnvironmentView":
        """Reduce over every source record that states an environment.

        Two places can state it and both are read.  ``cache_provenance`` carries
        it for the tools the provider snapshot cache covers — place identity and
        route lookups, never a flight search — and the provider's own sanitized
        payload carries it for everything else.  The two must agree for one
        response, which is what stops the disclosure depending on whether Redis
        happened to hold the answer.  Reading only the first is why this
        disclosure never fired for the case it was built for: a Duffel flight
        search never goes through that cache, so every leg of a sandbox itinerary
        reported production by silence, on screen and in the exported PDF.

        Total by construction: a stored Bundle must reduce, never raise.  The
        obligation to *state* an environment is enforced where the record is
        written, not where a finished plan is read.
        """

        return cls(
            has_sandbox_evidence=any(
                _source_data_environment(source) == "sandbox"
                for source in bundle.fact_snapshot.source_records
            )
        )

    @property
    def sandbox_note(self) -> Optional[str]:
        """The sentence to state, or ``None`` when every response was production."""

        return SANDBOX_EVIDENCE_NOTE if self.has_sandbox_evidence else None


def _source_data_environment(source: Any) -> Optional[str]:
    provenance = getattr(source, "cache_provenance", None)
    if provenance is not None:
        return provenance.data_environment
    return _declared(getattr(source, "snapshot", None))


def _declared(snapshot: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not isinstance(snapshot, Mapping):
        return None
    for payload in (snapshot, snapshot.get("sanitized_result")):
        if not isinstance(payload, Mapping):
            continue
        value = payload.get("data_environment")
        if isinstance(value, str) and value in _VALID:
            return value
    return None

"""What the Run failed to cover, said once, in product language.

The structured disclosure on the Bundle names research domains.  A traveller does
not read domain enums, so exactly one place turns them into sentences — the same
arrangement ``_LONG_DISTANCE_GAP_NOTES`` already uses for the round-trip gap
notes, and for the same reason: the formal report and the exported PDF have to say
this in identical words or the plan contradicts itself between two of its own
renderings.

The vocabulary is deliberately narrow.  A reason code, a provider name or a worker
name on this surface reads either as blame aimed at a supplier or as an internal
error report, and neither is what happened: the Run looked, and came back without
anything it was willing to put in the plan.
"""

from __future__ import annotations

from typing import List

from .delivery_bundle import ResearchDomain, RunCoverageDisclosure


_DOMAIN_NAMES = {
    ResearchDomain.VISIT: "景点与体验",
    ResearchDomain.DINING: "餐饮",
    ResearchDomain.LODGING: "住宿",
    ResearchDomain.LOCAL_TRANSPORT: "市内交通",
    ResearchDomain.LONG_DISTANCE_TRANSPORT: "长途交通",
}


def coverage_disclosure_notes(disclosure: RunCoverageDisclosure) -> List[str]:
    """One short sentence per domain that came back with nothing usable."""

    return [
        f"本次规划在{_DOMAIN_NAMES[domain]}上未取得可用结果，方案中相关条目相应减少。"
        for domain in disclosure.domains_without_results
        if domain in _DOMAIN_NAMES
    ]

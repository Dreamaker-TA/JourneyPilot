"""How old is too old for a committed upstream snapshot.

The 12306 station table ships in the repo so station resolution is deterministic
and offline (``mcp_servers/rail/README.md``).  That is the right trade, and it has
one cost: nothing expires.  A snapshot whose cities have since been renamed or
regrouped does not fail — it silently narrows the station scope a leg is bound
against, and the run reports "no trains on your endpoints" for a train that runs.

So the age becomes a stated fact with a stated criterion.  Version comparison is
the real judgement: it answers "is this the table upstream is serving now?"  Age
is a proxy that only answers "how long since anyone asked".  When upstream
publishes a version, that wins; when it does not, the verdict says which question
it actually answered rather than implying the stronger one.

Pure by construction — no clock, no filesystem, no config lookup.  The caller
supplies now, the parsed metadata and the threshold, which is what lets a preflight
runner, an HTTP probe and a test all reach the same verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, Mapping, Optional

FreshnessVerdict = Literal["current", "stale", "unknown"]
FreshnessCriterion = Literal["upstream_version", "age", "unreadable"]


@dataclass(frozen=True)
class SnapshotFreshness:
    """One snapshot's verdict, and which question the verdict answers."""

    verdict: FreshnessVerdict
    criterion: FreshnessCriterion
    detail: str
    age_days: Optional[int] = None
    declared_version: Optional[str] = None
    upstream_version: Optional[str] = None

    @property
    def is_current(self) -> bool:
        return self.verdict == "current"


def _parse_fetched_at(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def station_snapshot_freshness(
    metadata: Optional[Mapping[str, Any]],
    *,
    now: date,
    max_age_days: int,
    upstream_version: Optional[str] = None,
) -> SnapshotFreshness:
    """Judge a committed station snapshot from its own metadata.

    ``unknown`` is never treated as ``current``: an unreadable or undated sidecar
    means nobody can say how old the table is, and a governance check that reports
    the reassuring answer when it has no answer is worse than no check.
    """

    if not isinstance(metadata, Mapping) or not metadata:
        return SnapshotFreshness(
            verdict="unknown",
            criterion="unreadable",
            detail="站表 meta 缺失或不可解析：无法判断快照年龄",
        )

    declared = str(metadata.get("station_version") or "").strip() or None
    upstream = str(upstream_version or "").strip() or None
    if declared is not None and upstream is not None:
        matched = declared == upstream
        return SnapshotFreshness(
            verdict="current" if matched else "stale",
            criterion="upstream_version",
            detail=(
                f"station_version 与上游一致（{declared}）"
                if matched
                else f"station_version 落后于上游：本地 {declared}，上游 {upstream}"
            ),
            declared_version=declared,
            upstream_version=upstream,
        )

    fetched_at = _parse_fetched_at(metadata.get("fetched_at"))
    if fetched_at is None:
        return SnapshotFreshness(
            verdict="unknown",
            criterion="unreadable",
            detail="站表 meta 没有可解析的 fetched_at：无法判断快照年龄",
            declared_version=declared,
            upstream_version=upstream,
        )

    age_days = (now - fetched_at).days
    fresh = age_days <= max_age_days
    return SnapshotFreshness(
        verdict="current" if fresh else "stale",
        criterion="age",
        detail=(
            f"抓取于 {fetched_at.isoformat()}，{age_days} 天前"
            f"（阈值 {max_age_days} 天{'，未超龄' if fresh else '，已超龄'}）"
            + ("；上游未发布版本号，这里判的是年龄不是一致性" if declared is None else "")
        ),
        age_days=age_days,
        declared_version=declared,
        upstream_version=upstream,
    )

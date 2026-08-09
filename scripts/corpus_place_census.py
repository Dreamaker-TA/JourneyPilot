"""How much of the indexed corpus can nominate a place at all.

This answers the gate question of the「让知识库真正参与结果生成」topic: a chunk can
only ever *nominate* — identity stays the Provider's — so a corpus whose chunks
name no resolvable places cannot influence selection no matter what the prompt
says, and changing the prompt would be a guaranteed no-op.

**Criterion.** A chunk names a concrete place iff at least one string proposed by
``rag.place_mentions.propose_place_mentions`` resolves, through the repo's own
place path (``search_nominatim_raw`` -> ``normalize_nominatim_place`` ->
``is_concrete_visit_place`` / ``is_concrete_dining_place``), to a Provider identity
that

1. actually bears that name (``mention_matches_name`` — Nominatim's index is
   fuzzy, and without this a junk string matches an unrelated record), and
2. lies within ``SCOPE_KM`` of one of the destinations the corpus is about (the
   topic's own truth definition is "属于本次目的地的具体地点").

Each centre carries the country its ``/search`` calls are narrowed to, because
the corpus spans two of them. The country is a search parameter, not part of the
criterion: a corpus about Tokyo scored with a hardcoded ``cn`` filter would
report zero and the number would stop being comparable with the Chinese half.

Two numbers come out, because they answer different questions. OSM types a
motorway, a rail line and a canal as perfectly real places, and they pass the
pipeline's ``visit`` rule (visit = not dining, not lodging) — but nobody plans a
day around 杭州绕城高速公路. So a **stop** (anything not under OSM's
``highway``/``railway``/``aeroway``/``waterway`` keys) is counted separately from
infrastructure. The stop number is the one that bounds what a prompt change could
possibly achieve.

**Why it is recomputable.** The proposer is deterministic and lives in the
product tree, so it cannot drift away from what the runtime funnel measures.
Provider answers are memoized to ``--out``, and ``search_nominatim_raw`` memoizes
the raw bodies in Redis for a week on top of that, so a re-run costs nothing until
the corpus or the proposer changes.

**Cost control.** The metric is existential per chunk ("at least one"), so a
chunk stops at its first stop-grade hit. Proposals are ranked clean-edge-first by
``propose_place_mentions``, and a chunk gives up after ``--cap`` lookups — a
chunk that hits the cap is reported as ``capped`` rather than silently as "names
no place". Chunks are visited in a seeded shuffle, so an interrupted run is still
an unbiased sample of the corpus rather than a prefix of whatever order the table
returned.

The public Nominatim instance answers 429 under sustained load: this runs at a
slower courtesy interval than production and waits rather than pressing.

    python scripts/corpus_place_census.py --limit 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from travel_agent.config import get_settings  # noqa: E402
from travel_agent.infrastructure.database import get_session_factory  # noqa: E402
from travel_agent.rag.place_mentions import (  # noqa: E402
    mention_matches_name,
    propose_place_mentions,
)
from travel_agent.rag.collections import FACTORY_KNOWLEDGE_COLLECTIONS  # noqa: E402
from travel_agent.services.nominatim_place_search import (  # noqa: E402
    NominatimPlaceSearchError,
    is_concrete_dining_place,
    is_concrete_visit_place,
    normalize_nominatim_place,
    search_nominatim_raw,
)

# The local factory corpus, read from the one place that names it. This census scores
# the optional local corpus, so it deliberately excludes the per-user uploaded
# library that ``grounding_corpus`` adds at retrieval time: that library differs per
# machine and per user, and folding it in would make criterion D irreproducible.
COLLECTIONS = FACTORY_KNOWLEDGE_COLLECTIONS

# OSM's own top-level keys for transport infrastructure.
INFRASTRUCTURE_KEYS = ("highway", "railway", "aeroway", "waterway")

SEED = 20260801
COURTESY_INTERVAL_SECONDS = 2.5
BACKOFF_SECONDS = (30.0, 120.0, 300.0)

# The destinations the corpus is about, each with the country its Provider
# lookups are narrowed to. The country is *not* part of the criterion — it only
# narrows ``/search`` — but it has to travel with the centre, because a corpus
# spanning two countries cannot be scored by one hardcoded filter.
DEFAULT_CENTRES = ("杭州市:cn", "苏州市:cn", "绍兴市:cn", "東京都:jp")


def _normalize(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi, d_lambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def place_bears_name(mention: str, place: dict) -> bool:
    """The Provider record must be one of the names the corpus actually wrote."""
    for candidate in (place.get("name"), *(place.get("aliases") or ())):
        if mention_matches_name(mention, str(candidate or "")):
            return True
    return False


async def _search(query: str, country_code: str):
    last = ""
    for delay in (*BACKOFF_SECONDS, None):
        try:
            return await search_nominatim_raw(query, country_code=country_code, limit=5)
        except NominatimPlaceSearchError as exc:
            last = str(exc)
            if delay is None:
                break
            print(f"    provider refused ({query!r}); waiting {delay:.0f}s", flush=True)
            await asyncio.sleep(delay)
    raise NominatimPlaceSearchError(last, code="provider_unavailable")


def parse_centre(spec: str) -> tuple[str, str]:
    """``"東京都:jp"`` -> ``("東京都", "jp")``; a bare name stays Chinese."""
    name, _, country = spec.partition(":")
    return name.strip(), (country.strip() or "cn").casefold()


async def load_centres(specs: tuple[str, ...]) -> dict[str, tuple[float, float, str]]:
    """Destination centres, resolved from the Provider rather than hand-copied.

    A hardcoded coordinate table is a second source of truth that goes stale in
    silence — the shape of 口子 9 and 口子 26.
    """
    centres: dict[str, tuple[float, float, str]] = {}
    for spec in specs:
        name, country = parse_centre(spec)
        observation = await _search(name, country)
        place = normalize_nominatim_place(observation.items[0])
        centres[name] = (place["latitude"], place["longitude"], country)
        print(
            f"centre {name} [{country}] -> {place['name']} "
            f"@ {place['latitude']},{place['longitude']}",
            flush=True,
        )
    return centres


async def resolve(mention: str, country: str, centres: dict, scope_km: float) -> dict:
    """One mention against one country's Provider index.

    Only centres in ``country`` are candidates for the scope test: a Hangzhou
    centre can never be within ``scope_km`` of a Japanese record anyway, and
    saying so here keeps the reported distance meaningful.
    """
    try:
        observation = await _search(mention, country)
    except NominatimPlaceSearchError as exc:
        return {"error": str(exc)}
    named = [
        place
        for item in observation.items
        if (place := normalize_nominatim_place(item)) is not None
        and place["provider_country_code"] == country
        and place_bears_name(mention, place)
    ]
    local_centres = {
        name: (lat, lon) for name, (lat, lon, cc) in centres.items() if cc == country
    }

    def project(places: list) -> list:
        rows = []
        for place in places:
            distance, city = min(
                (
                    (haversine_km(place["latitude"], place["longitude"], lat, lon), name)
                    for name, (lat, lon) in local_centres.items()
                ),
                default=(float("inf"), ""),
            )
            rows.append(
                {
                    "place_id": place["place_id"],
                    "name": place["name"],
                    "type": place["provider_place_type"],
                    "km": round(distance, 1),
                    "city": city,
                    "in_scope": distance <= scope_km,
                    "infrastructure": place["provider_place_type"].split(";")[0]
                    in INFRASTRUCTURE_KEYS,
                }
            )
        return rows

    return {
        "returned": len(observation.items),
        "named": len(named),
        "visit": project([p for p in named if is_concrete_visit_place(p)]),
        "dining": project([p for p in named if is_concrete_dining_place(p)]),
    }


def in_scope_hits(entry: dict) -> list:
    if not entry or "error" in entry:
        return []
    hits = list(entry.get("visit") or ()) + list(entry.get("dining") or ())
    return [hit for hit in hits if hit["in_scope"]]


def report(rows: list, resolutions: dict, total: int) -> None:
    n = len(rows)
    if not n:
        return
    stop = sum(1 for row in rows if row["hit_stop"])
    scoped = sum(1 for row in rows if row["hit_stop"] or row["hit_infrastructure"])
    anywhere = sum(1 for row in rows if row["hit_anywhere"])
    capped = sum(1 for row in rows if row["capped"])
    errors = sum(1 for entry in resolutions.values() if "error" in entry)
    margin = 1.96 * math.sqrt(max(stop / n * (1 - stop / n), 1e-9) / n)
    print(
        f"\n=== {n}/{total} chunks | lookups={len(resolutions)} errors={errors} capped={capped}\n"
        f"    names an in-scope STOP:          {stop}/{n} = {stop / n:.1%} (95% CI +/-{margin:.1%})\n"
        f"    names in-scope stop or infra:    {scoped}/{n} = {scoped / n:.1%}\n"
        f"    names any concrete place at all: {anywhere}/{n} = {anywhere / n:.1%}",
        flush=True,
    )
    # Per-collection, because the corpus is not one thing: the two halves are
    # chunked differently (500-char windows vs contextual sentence slices), and a
    # corpus-wide number that moved could otherwise be read as a density gain
    # when it was really a change in the mix.
    _breakdown(rows, "collection", lambda row: row["collection"])
    # Per manifest batch, which is also per destination. This one matters more:
    # the criterion resolves names through a Provider that answers in Japanese
    # for Japan, so it is structurally blind to the Tokyo half .
    # One corpus-wide number silently averages a measurable half with an
    # unmeasurable one.
    batches = load_batches()
    if batches:
        _breakdown(rows, "batch", lambda row: batches.get(row["source"], "(未登记批次)"))


def _breakdown(rows: list, label: str, key) -> None:
    buckets: dict[str, list] = {}
    for row in rows:
        buckets.setdefault(key(row), []).append(row)
    print(f"    -- 按{label}", flush=True)
    for name, group in sorted(buckets.items()):
        hits = sum(1 for row in group if row["hit_stop"])
        print(
            f"      {name:<26} {hits:>4}/{len(group):<4} = {hits / len(group):>6.1%}",
            flush=True,
        )


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_batches(root: str = "") -> dict[str, str]:
    """``doc_source`` -> which manifest batch listed it.

    The mapping is the manifest itself rather than a table written here, so a
    batch cannot drift away from the titles it actually indexed.
    """
    # Anchored to the repo, not to the caller's cwd: the census is run from
    # wherever, and a batch breakdown that silently vanishes outside the repo
    # root reads as "no batches" rather than as a broken path.
    root = root or os.path.join(_REPO_ROOT, "data", "corpus")
    prefixes = {"wikipedia": "wiki", "wikivoyage": "voyage"}
    batches: dict[str, str] = {}
    if not os.path.isdir(root):
        return batches
    for name in sorted(os.listdir(root)):
        if not name.endswith(".txt"):
            continue
        project = next((p for p in prefixes if name.startswith(p)), None)
        if project is None:
            continue
        label = name[:-4]
        with open(os.path.join(root, name), encoding="utf-8") as handle:
            for line in handle:
                title = line.strip()
                if title and not title.startswith("#"):
                    batches[f"{prefixes[project]}-zh-{title}"] = label
    return batches


async def load_chunks() -> list[dict]:
    from sqlalchemy import text

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text(
                "select id, collection, source, content from knowledge_chunks "
                "where collection = any(:collections) order by id"
            ),
            {"collections": list(COLLECTIONS)},
        )
        return [
            {"id": str(row[0]), "collection": row[1], "source": row[2], "content": row[3]}
            for row in result.fetchall()
        ]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="tmp/corpus-place-census.json")
    parser.add_argument("--cap", type=int, default=12, help="max lookups per chunk")
    parser.add_argument("--scope-km", type=float, default=60.0)
    parser.add_argument("--limit", type=int, default=0, help="stop after N chunks (0 = all)")
    parser.add_argument(
        "--centre",
        action="append",
        default=None,
        help="destination the corpus is about, as NAME[:COUNTRY]; repeatable",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="re-report a finished census from --out without asking the Provider anything",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=COURTESY_INTERVAL_SECONDS,
        help=(
            "seconds between Provider calls. Pacing only — it changes how long the "
            "census takes, never what it reports."
        ),
    )
    args = parser.parse_args()

    if args.report_only:
        state = json.load(open(args.out, encoding="utf-8"))
        rows = state.get("chunks", [])
        # The corpus this census ran against, not whatever is in the table now:
        # re-reporting an old census after the corpus grew would otherwise print
        # a denominator that never applied to it.
        report(rows, state.get("resolutions", {}), state.get("corpus_total", len(rows)))
        return

    get_settings().geocoding.min_interval_seconds = args.interval
    centres = await load_centres(tuple(args.centre or DEFAULT_CENTRES))
    # Fixed order, so the census is deterministic no matter how --centre was typed.
    countries = tuple(dict.fromkeys(cc for _, _, cc in centres.values()))

    chunks = await load_chunks()
    order = list(range(len(chunks)))
    random.Random(SEED).shuffle(order)

    state = json.load(open(args.out, encoding="utf-8")) if os.path.exists(args.out) else {}
    resolutions: dict = state.get("resolutions", {})
    rows: list = state.get("chunks", [])
    visited = {row["id"] for row in rows}
    print(f"chunks={len(chunks)} resuming: visited={len(rows)} lookups={len(resolutions)}", flush=True)

    def flush() -> None:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "resolutions": resolutions,
                    "chunks": rows,
                    "corpus_total": len(chunks),
                },
                handle,
                ensure_ascii=False,
            )

    for index in order:
        chunk = chunks[index]
        if chunk["id"] in visited:
            continue
        if args.limit and len(rows) >= args.limit:
            break
        proposals = [m.text for m in propose_place_mentions(chunk["content"])]
        hit_stop = hit_infrastructure = None
        hit_anywhere = False
        for mention in proposals[: args.cap]:
            # Countries in a fixed order, and a stop in the first one skips the
            # rest. That short-circuit cannot change any of the three reported
            # aggregates: it only ever fires once ``hit_stop`` is set, and a stop
            # already implies both weaker verdicts.
            for country in countries:
                key = f"{country}|{mention}"
                if key not in resolutions or "error" in resolutions[key]:
                    resolutions[key] = await resolve(
                        mention, country, centres, args.scope_km
                    )
                entry = resolutions[key]
                if not hit_anywhere and (entry.get("visit") or entry.get("dining")):
                    hit_anywhere = True
                scoped = in_scope_hits(entry)
                stops = [hit for hit in scoped if not hit["infrastructure"]]
                if stops:
                    hit_stop = mention
                    break
                if scoped and hit_infrastructure is None:
                    hit_infrastructure = mention
            if hit_stop:
                # Only a stop ends the chunk: stopping at an infrastructure hit
                # recorded "this chunk names roads only" for chunks whose museum
                # merely sat further down the ranking.
                break
        rows.append(
            {
                "id": chunk["id"],
                "collection": chunk["collection"],
                "source": chunk["source"],
                "proposed": len(proposals),
                "capped": len(proposals) > args.cap,
                "hit_stop": hit_stop,
                "hit_infrastructure": hit_infrastructure,
                "hit_anywhere": hit_anywhere or bool(hit_stop or hit_infrastructure),
            }
        )
        if len(rows) % 10 == 0:
            flush()
            report(rows, resolutions, len(chunks))

    flush()
    report(rows, resolutions, len(chunks))


if __name__ == "__main__":
    asyncio.run(main())

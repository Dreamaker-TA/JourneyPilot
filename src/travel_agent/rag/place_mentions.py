"""What places a knowledge-base chunk names, and how far the run took them.

The knowledge base cannot put a place into an itinerary by itself: a Candidate's
``place_id`` is pinned to a Provider result by the packet schema, and that is
deliberate (identity is never the model's to author). What a chunk
*can* do is **nominate**: name a place the worker then resolves through
``global_place_search``, exactly the way a place the user asked for by name gets
in. Nomination is therefore a chain with four hops::

    a chunk names X  ->  the worker looks X up  ->  X lands in
    eligible_place_options  ->  X becomes a Candidate

Five rounds of measurement showed the chain producing nothing without anybody
noticing, because **no number anywhere described it**. This module produces those
numbers and nothing else: it decides nothing, filters nothing and blocks nothing.

Two halves, with very different standing:

* ``propose_place_mentions`` is the **denominator**, and it is a *recall
  heuristic*. It over-generates on purpose — it proposes anything shaped like a
  named place, including strings that are not places at all. It must never be
  read as "this chunk contains these places"; it means "these are the strings
  worth asking a Provider about". Precision is not its job and it does not have
  any: only a Provider can say whether a string names a real place, and that is
  the same boundary the rest of the pipeline holds.
* the numerator is read off what actually happened in the run — the lookups the
  worker sent, the options the Gateway admitted, the Candidates the packet
  carried — so hops 2-4 are exact.

**A name comparison joins the first hop only** (``mention_matches_name``:
does this proposal name the thing the worker asked the Provider about?). Both
sides of that comparison are written in the corpus's own script, because the
worker types the query out of the chunk it just read, so a spelling gap cannot
open there.

Everything downstream of the lookup is joined by ``place_id`` instead, through
:class:`PlaceLookup` — *which call did this identity come out of, and was that
call nominated*. That is the provenance the run already has, and it is what
makes the measurement script-agnostic. Comparing names past the lookup is the
pairing that failed: the corpus says 浅草寺, OSM answers 淺草寺 or ``Tokyo
National Museum``, and every Japanese destination read zero at hops 3-5 while
the packet plainly carried the Candidates. A folding table would have papered
over the Chinese half of that and still lost the English half; the identity the
Provider minted never had the problem in the first place.

Every function here is pure: no clock, no provider, no I/O, no global state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

# Category words a concrete, visitable place name ends with. This is a *recall*
# vocabulary, not a definition of what a place is — a name it misses is a
# nomination this measurement will under-count, and a name it wrongly proposes
# costs one Provider lookup that comes back empty. Neither can admit anything,
# so the list is allowed to be generous and is allowed to be incomplete.
#
# Region words (市 / 省 / 区 / 县 / 州) are deliberately absent, but nothing here
# relies on that: "杭州" is rejected because the Provider types it
# ``boundary;administrative;city``, which ``is_administrative_provider_type``
# already knows is an area rather than a stop.
CONCRETE_PLACE_SUFFIXES: tuple[str, ...] = tuple(
    sorted(
        {
            # 宗教与古迹
            "寺", "庙", "观", "祠", "陵", "墓", "塔", "宫", "殿", "庵", "庄",
            # 自然与园林
            "园", "公园", "花园", "植物园", "动物园", "山庄", "湖", "山", "峰",
            "岭", "洞", "泉", "岛", "湾", "溪", "潭", "瀑布", "湿地", "濕地",
            "江", "河", "港", "温泉",
            # 建筑与文化设施
            "楼", "阁", "亭", "台", "桥", "堂", "馆", "博物馆", "纪念馆",
            "美术馆", "图书馆", "剧院", "剧场", "书院", "故居", "遗址", "印社",
            "碑林", "城墙", "城楼", "门", "府", "宅", "院", "寺塔",
            # 街区与商业
            "街", "巷", "弄", "坊", "路", "大道", "广场", "商城", "市场",
            "夜市", "美食街", "步行街", "古镇", "古村", "村", "老街",
            # 交通
            "站", "车站", "机场", "码头", "隧道",
            # 主题与度假
            "乐园", "游乐园", "度假区", "景区", "风景区", "水库", "农庄", "茶园",
            # 餐饮与住宿
            "酒店", "饭店", "宾馆", "餐厅", "茶楼", "菜馆", "食府", "客栈",
            "面馆", "酒家",
        },
        key=len,
        reverse=True,
    )
)

# Characters that cannot occur inside a place name, used to find its left edge.
# Deliberately narrow: an over-wide set decapitates real names.  Carrying 中 / 周 /
# 绍 / 上 for the sake of 中间 / 周边 / 介绍 / 以上 cuts 中国茶叶博物馆, 周庄古镇,
# 绍兴北站 and 上海虹桥站 at their first character.  Prefer leaving a verb attached
# over cutting a name in half.
#
# It follows that the set is *incomplete on purpose* and always will be — no
# character list can enumerate every verb that can sit in front of a name, so
# the corpus's own "该段落介绍苏州博物馆" comes out as one mention with the verb
# still on it. That is deliberately not repaired here by generating stripped
# variants: variants would triple the denominator with noise, and
# ``mention_matches_name`` already joins such a mention to the Provider's
# "苏州博物馆", so the chain stays connected end to end without them.
_NAME_BOUNDARY_CHARS = frozenset(
    "的和与及有是在为等或从把被让即将也都还很更最就才只再又如而但所因由此这那些"
    "位于拥有现有包括始建成其多余约共计据说达到超过并无"
    "一二三四五六七八九十百千万亿第年月日号处座条例比称作叫做"
)

# Anything that is not a CJK ideograph, a letter or a digit ends a segment: a
# place name never spans punctuation, and the corpus writes its lists with 、.
_SEGMENT_SPLIT = re.compile(r"[^一-鿿㐀-䶿 A-Za-z0-9]+".replace(" ", ""))

# A proposal shorter than this cannot identify anything (西湖 is the shortest
# real name in the corpus); longer than this is a sentence, not a name.
_MIN_MENTION_CHARS = 2
_MAX_MENTION_CHARS = 10


@dataclass(frozen=True)
class PlaceMention:
    """One string a chunk might be naming a place with.

    ``left_edge_is_clean`` records whether the name's left edge was found by a
    boundary character or the start of a segment, rather than forced by
    ``_MAX_MENTION_CHARS``. A forced edge means the proposal is a truncated
    sentence far more often than it is a name, which is why callers that can
    only afford a few Provider lookups should spend them on the clean ones first.
    """

    text: str
    left_edge_is_clean: bool


def propose_place_mentions(text: str) -> tuple[PlaceMention, ...]:
    """Every string in ``text`` shaped like the name of a visitable place.

    Ranked: clean left edge first, then longer before shorter, so a caller that
    truncates the list keeps the most place-like proposals. Deterministic — the
    same text always yields the same list in the same order, which is what makes
    a corpus census recomputable.
    """

    proposals: dict[str, bool] = {}
    # A wiki link renders as "大明山 (浙江)|大明山"; the bar separates two
    # spellings of one name rather than joining two names.
    for segment in _SEGMENT_SPLIT.split(str(text or "").replace("|", " ")):
        if not segment:
            continue
        for suffix in CONCRETE_PLACE_SUFFIXES:
            start = 0
            while (hit := segment.find(suffix, start)) >= 0:
                end = hit + len(suffix)
                left = hit
                while (
                    left > 0
                    and (end - left) < _MAX_MENTION_CHARS
                    and segment[left - 1] not in _NAME_BOUNDARY_CHARS
                ):
                    left -= 1
                mention = segment[left:end]
                clean = left == 0 or segment[left - 1] in _NAME_BOUNDARY_CHARS
                if _is_proposable(mention):
                    proposals[mention] = proposals.get(mention, False) or clean
                start = end
    return tuple(
        PlaceMention(text=mention, left_edge_is_clean=clean)
        for mention, clean in sorted(
            proposals.items(), key=lambda item: (not item[1], -len(item[0]), item[0])
        )
    )


def _is_proposable(mention: str) -> bool:
    return (
        _MIN_MENTION_CHARS <= len(mention) <= _MAX_MENTION_CHARS
        and not mention.isdigit()
        # A bare category word ("车站", "公园") names no particular place.
        and mention not in CONCRETE_PLACE_SUFFIXES
    )


def _normalize(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def mention_matches_name(mention: str, name: str) -> bool:
    """Whether ``name`` is another writing of the nomination ``mention``.

    Containment either way, because the two ends disagree in both directions: a
    proposal can carry a verb the query does not ("该段落介绍苏州博物馆" vs
    "苏州博物馆") and can be missing a character the query has ("州工艺美术博物馆"
    vs "杭州工艺美术博物馆"). The length floor is what keeps that from collapsing
    into "matches anything": without it a record literally named 公路 would
    confirm every proposal containing 公路.

    **This is a comparison between two strings written by the same side** — the
    chunk and the query the worker typed out of it, or the chunk and a name a
    corpus census asked a Provider for by that exact name. It is deliberately not
    used to decide whether a Provider's answer is "the same place" as a mention;
    see the module docstring for why that job belongs to ``place_id``.
    """

    left, right = _normalize(mention), _normalize(name)
    if len(left) < _MIN_MENTION_CHARS or len(right) < _MIN_MENTION_CHARS:
        return False
    if not (left == right or left in right or right in left):
        return False
    return min(len(left), len(right)) * 2 >= max(len(left), len(right))


def mention_matches_any(mention: str, names: Iterable[object]) -> bool:
    return any(mention_matches_name(mention, str(name)) for name in names)


def query_carries_mention(query: str, mention: str) -> bool:
    """Whether the worker typed this nomination into that Provider query.

    A query is **composed**, not canonical: the worker routinely writes the name
    it read plus a romanisation or a city — ``浅草寺 Sensoji``, ``灵隐寺 杭州``,
    ``皇居 东京 Imperial Palace``. Against the whole string the length floor in
    :func:`mention_matches_name` then rejects the nomination for being too short
    a fraction of it, and where it lands is pure accident: ``灵隐寺 杭州`` passed
    by one character while ``浅草寺 Sensoji`` failed, so an international run read
    ``queried=0`` in a round that had asked about both of its temples by name.

    So the comparison runs per segment, split exactly the way the proposer splits
    a chunk — no new rule and no new threshold, just the recognition that a query
    holding two names is holding two names. The floor keeps doing its job inside
    each segment, which is where it was always meant to apply: ``公路`` still
    cannot confirm ``杭金衢高速公路``.
    """

    if mention_matches_name(mention, query):
        return True
    return any(
        mention_matches_name(mention, segment)
        for segment in _SEGMENT_SPLIT.split(str(query or ""))
        if segment
    )


@dataclass(frozen=True)
class ProviderIdentity:
    """One place a Provider returned: the id it minted, and what it calls it.

    ``place_id`` is what the whole chain downstream is keyed on — it is the only
    thing about a place that every layer agrees on, and it is the same value the
    packet schema pins a Candidate's identity to. ``name`` is carried for the log
    line and nothing else; nothing is ever decided by comparing it.
    """

    place_id: str
    name: str


@dataclass(frozen=True)
class PlaceLookup:
    """One place-Provider call: what it was asked, and which identities came back.

    This object is what turns the funnel into a chain instead of four independent
    string comparisons. A chunk nominates by naming something the worker then
    *asks about*; whatever that particular call produced is downstream of that
    nomination, whatever the Provider chose to call it.

    A call that came back empty still belongs here with ``identities=()``: "asked
    and found nothing" and "never asked" are the two states this measurement most
    needs to tell apart, because only the second one means the prompt failed.
    """

    query: str
    identities: tuple[ProviderIdentity, ...] = ()


@dataclass(frozen=True)
class PlaceFunnel:
    """One worker round's chunk-to-Candidate nomination chain, counted.

    **The hops are counted in places, not in mention strings.** The first real
    run made the difference matter: one chunk sentence about 西湖 yields
    「杭州西湖」「距离西湖」「西湖」「请见西湖」「中西湖」 as five separate proposals,
    all naming one place. Counting the mention side turned one nominated place
    into ``admitted=5`` — a number that reads like success and is off by 5x.

    Hops 3-5 hold ``ProviderIdentity`` rather than names, so two different places
    the Provider happens to call the same thing count as two, and one place the
    Provider spells differently from the corpus counts as one. ``queried`` stays
    a list of query strings: at that hop no identity exists yet, and a lookup that
    found nothing still has to be visible.

    Each hop is a subset of the one before it, by construction rather than by
    coincidence — so a drop between two numbers is always a real loss at that
    step, and the chain can never again report a later hop as zero while a Candidate
    from it sits in the packet.

    ``mentions`` stays on the proposal side because it is the denominator, and
    it is the one heuristic number here — see the module docstring.
    """

    injected_chunks: int
    mentions: tuple[str, ...]
    queried: tuple[str, ...]
    resolved: tuple[ProviderIdentity, ...]
    eligible: tuple[ProviderIdentity, ...]
    admitted: tuple[ProviderIdentity, ...]

    def as_log_line(self) -> str:
        def names(identities: Sequence[ProviderIdentity]) -> list[str]:
            """Provider names, with repeats folded into ``名字×N``.

            One query routinely returns several *distinct* places the Provider
            spells the same way — 西湖 comes back as the lake, the district and
            the scenic area. Those are genuinely three identities and the counts
            must stay 3, but printing 西湖 three times reads like a bug in the
            log rather than a fact about the Provider.
            """
            counted: dict[str, int] = {}
            for identity in identities:
                counted[identity.name] = counted.get(identity.name, 0) + 1
            return [
                name if count == 1 else f"{name}×{count}"
                for name, count in counted.items()
            ]

        return (
            f"chunks={self.injected_chunks} mentions={len(self.mentions)} "
            f"queried={len(self.queried)} resolved={len(self.resolved)} "
            f"eligible={len(self.eligible)} admitted={len(self.admitted)}"
            f" | queried={list(self.queried)} eligible={names(self.eligible)}"
            f" admitted={names(self.admitted)}"
        )


def measure_place_funnel(
    *,
    injected_chunk_texts: Sequence[str],
    lookups: Sequence[PlaceLookup],
    selectable_place_ids: Iterable[str],
    admitted_place_ids: Iterable[str],
) -> PlaceFunnel:
    """Count how many places this round's chunks nominated, and how far each got.

    Everything but ``injected_chunk_texts`` is an observation of what the run
    actually did, so hops 2-5 are exact; only the denominator is heuristic.

    The one judgement call is which lookups the chunks caused, and it is made
    once, against the query string the worker typed. From there the chain follows
    ``place_id``.
    """

    mentions = tuple(
        dict.fromkeys(
            mention.text
            for text in injected_chunk_texts
            for mention in propose_place_mentions(text)
        )
    )
    nominated = [
        lookup
        for lookup in lookups
        if any(query_carries_mention(lookup.query, mention) for mention in mentions)
    ]

    resolved: dict[str, ProviderIdentity] = {}
    for lookup in nominated:
        for identity in lookup.identities:
            resolved.setdefault(identity.place_id, identity)

    selectable = set(selectable_place_ids)
    eligible = {
        place_id: identity
        for place_id, identity in resolved.items()
        if place_id in selectable
    }
    admitted = set(admitted_place_ids)

    return PlaceFunnel(
        injected_chunks=len(injected_chunk_texts),
        mentions=mentions,
        queried=tuple(dict.fromkeys(lookup.query for lookup in nominated)),
        resolved=tuple(resolved.values()),
        eligible=tuple(eligible.values()),
        admitted=tuple(
            identity for place_id, identity in eligible.items() if place_id in admitted
        ),
    )


def chunk_texts_by_collection(
    injected_rag_sources: Mapping[str, Mapping[str, object]],
) -> dict[str, tuple[str, ...]]:
    """The full chunk bodies this round printed into the prompt, kept apart by collection.

    ``rag_chunk_source_records`` already stores the complete chunk under
    ``snapshot.content`` — the contract is "完整 RAG chunk，禁止裁剪成模型摘要" —
    so the funnel reads it from there rather than keeping a second copy.

    **Why the split is not cosmetic.** Two corpora now compete in one retrieval:
    the optional local factory corpus, and the library this user
    uploaded. A single pooled count answers "did the knowledge base contribute"
    and cannot answer "did *theirs*" — and theirs is the half a user can see,
    change, and be wrong about. One number covering both would report a healthy
    funnel on a run where their upload was never read, which is exactly the state
    this measurement exists to make impossible.

    ``snapshot.collection`` is a **logical** name by the time it gets here; the
    physical owner-scoped address is translated at the retrieval boundary and
    never reaches a source record.
    """

    grouped: dict[str, list[str]] = {}
    for record in injected_rag_sources.values():
        snapshot = record.get("snapshot") if isinstance(record, Mapping) else None
        if not isinstance(snapshot, Mapping):
            continue
        content = snapshot.get("content")
        if not (isinstance(content, str) and content.strip()):
            continue
        collection = str(snapshot.get("collection") or "unknown")
        grouped.setdefault(collection, []).append(content)
    return {collection: tuple(texts) for collection, texts in grouped.items()}


__all__ = [
    "CONCRETE_PLACE_SUFFIXES",
    "PlaceFunnel",
    "PlaceLookup",
    "PlaceMention",
    "ProviderIdentity",
    "chunk_texts_by_collection",
    "measure_place_funnel",
    "mention_matches_any",
    "mention_matches_name",
    "propose_place_mentions",
    "query_carries_mention",
]

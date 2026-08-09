"""Canonical knowledge collection contracts.

Public callers address logical collection names.  Physical PostgreSQL
collection names are derived server-side from the resolved product user and
must never cross the API or grounding boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, MutableMapping, Optional

USER_KNOWLEDGE_COLLECTION = "travel_knowledge"

# 出厂语料：由本地可选种子声明，对所有用户相同；公开仓库不携带这些数据。
FACTORY_KNOWLEDGE_COLLECTIONS = (
    "destinations",
    "local_culture",
    "visa_policies",
    "travel_tips",
)

_USER_ID_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def canonical_logical_collection(collection: str) -> str:
    """Return a public logical collection name or reject physical addressing."""

    logical = (collection or "").strip()
    if not logical:
        raise ValueError("collection 无效")
    if logical.startswith("u_") and "__" in logical:
        raise ValueError("collection 必须使用逻辑名称")
    return logical


def user_scoped_collection(user_id: str, logical_collection: str) -> str:
    """Resolve the one internal physical name for a user-owned collection."""

    cleaned_user = _USER_ID_SAFE_RE.sub("_", (user_id or "").strip())[:64]
    if not cleaned_user or cleaned_user == "anonymous":
        raise ValueError("user_id 无效")
    logical = canonical_logical_collection(logical_collection)
    return f"u_{cleaned_user}__{logical}"


@dataclass(frozen=True)
class GroundingCorpus:
    """一次接地检索要查哪些集合，以及每个物理名的公开逻辑名。

    这个对象存在的理由是**用户上传的资料库与出厂语料平等竞争**（专题文档 §4.1）：
    两者在同一次检索里一起被打分、一起融合、一起精排，由排名决定谁进 prompt，
    不是「出厂语料找不到才兜底查用户的」。所以它交出的是**一张探针清单**，
    而不是「主集合 + 备用集合」两档。

    它同时是「物理名不许越过接地边界」这条规则的唯一执行点：``probe_collections``
    里的用户集合是物理名（``u_<user>__travel_knowledge``，检索层需要它），而
    ``logical_name`` 是任何会被人看见的地方——摘要的 coverage、SourceRecord 的
    snapshot、日志——必须改用的名字。两者写在同一个对象上，是为了让「查哪张表」
    和「对外叫什么」不可能各自漂移。
    """

    probe_collections: tuple[str, ...]
    user_probe: Optional[str]
    _logical_by_probe: Mapping[str, str]

    def logical_name(self, probe_collection: str) -> str:
        """把一个探针名翻回公开逻辑名（出厂集合本来就是逻辑名，原样返回）。"""

        return self._logical_by_probe.get(probe_collection, probe_collection)

    @property
    def logical_collections(self) -> tuple[str, ...]:
        """本轮查过的集合，全部以公开逻辑名表示（可直接进摘要与日志）。"""

        return tuple(self.logical_name(probe) for probe in self.probe_collections)

    def is_user_owned(self, logical_collection: str) -> bool:
        """这个**逻辑**集合名是不是用户自己上传的那一个。"""

        return (
            self.user_probe is not None
            and logical_collection == USER_KNOWLEDGE_COLLECTION
        )


def grounding_corpus(user_id: str) -> GroundingCorpus:
    """出厂语料 + 这个用户自己上传的资料库，作为一份平等竞争的探针清单。

    ``user_id`` 解析不出一个真实用户时（空串、``anonymous``）只有出厂语料 ——
    那不是降级，是这台机器上确实没有第二份语料可查：用户库按 owner 物理隔离
    （``user_scoped_collection``），没有 owner 就没有集合。这一支必须显式表达为
    ``user_probe is None``，而不是让一个查不出东西的集合名混进清单：后者会让
    「用户没上传过任何东西」和「上传了但一段都没被召回」在读数上长得一样。

    **上面那句话对出厂那一半同样成立。** 出厂集合清单里的
    ``visa_policies`` 出厂就是空的（签证正文不含可安排的停留点，见
    本地种子说明里），却一直在被探针查 —— 每轮多印一行
    「向量 0 + lexical 0 → 融合 0」，而一次真正的接线断裂印出来的是同一行。
    所以探针清单只收**种子声称有货的**出厂集合（``factory_seed
    .seeded_factory_collections()`` 是这句话的唯一定义处，那里写了为什么是种子说了算、
    以及种子与库不一致时从哪儿看得见）。名字仍留在 ``FACTORY_KNOWLEDGE_COLLECTIONS``
    里作为逻辑语汇 —— 那张表回答的是「哪些集合名属于出厂语料」，不是「哪些有货」。

    这里的 import 写在函数体内：``factory_seed`` 依赖本模块的
    ``FACTORY_KNOWLEDGE_COLLECTIONS``，模块级互相 import 会成环。
    """

    from .factory_seed import seeded_factory_collections

    probes = list(seeded_factory_collections())
    logical_by_probe: dict[str, str] = {}
    user_probe: Optional[str] = None
    try:
        user_probe = user_scoped_collection(user_id, USER_KNOWLEDGE_COLLECTION)
    except ValueError:
        user_probe = None
    if user_probe is not None:
        probes.append(user_probe)
        logical_by_probe[user_probe] = USER_KNOWLEDGE_COLLECTION
    return GroundingCorpus(
        probe_collections=tuple(probes),
        user_probe=user_probe,
        _logical_by_probe=logical_by_probe,
    )


def relabel_to_logical_collections(
    docs: Iterable[MutableMapping[str, Any]], corpus: GroundingCorpus
) -> None:
    """把检索层写在 doc 上的探针名改成公开逻辑名，就地改。

    物理名（``u_<user>__travel_knowledge``）是检索层的地址，不是产品语汇：它会顺着
    ``doc["collection"]`` 流进 ``build_retrieval_summary`` 的 coverage、流进
    SourceRecord 的 snapshot，最后出现在用户点开的检查面上——而那一面连原始工具名
    都不许出现，更不用说一个带 owner id 的表内地址。

    **翻译只在接地边界做这一次。** 这个函数原先是 destination_researcher 的私有
    helper；当另一个 reader 也开始查同一份探针清单时，两条路径各抄一份就又是
    同一件事就又有了两套取值 —— 其中一份改了另一份没改，用户看到的是哪一份要看
    走的是哪条路。所以它作为探针清单定义处的方法，和 ``grounding_corpus`` 放在一起。
    """

    for doc in docs:
        probe = str(doc.get("collection") or "")
        if probe:
            doc["collection"] = corpus.logical_name(probe)

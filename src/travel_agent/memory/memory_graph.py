"""
Memory Graph (Infrastructure Layer)

知识图谱读写模块，使用 PostgreSQL 关系表模拟图结构：
  memory_entities  — 实体节点（今天只有 person 与 trait 两种）
  memory_relations — 关系边

设计特点：
  - 实体 upsert：基于 (user_id, name, entity_type) 唯一约束
  - 关系词表只有一个成员，且它必须有产出方（见 ``_PRODUCED_RELATIONS``）
  - 画像聚合：纯模板，无 LLM 调用，快速生成自然语言画像文本

**这里没有「图谱矛盾消解」。** 曾经有过一段按 (source, relation) 换 target 就给旧边
打 ``valid_until`` 墓碑的代码，以及一个「只对单值关系生效」的豁免集合 ``{has_budget}``。
产品侧唯一的写入方是 ``update_portrait``，它把 relation 写死成 ``has_trait`` ——
两个集合不相交，那段代码一次都没执行过，库里 ``valid_until`` 非空 0 行。第 40 迭代
把它连同 ``valid_until`` 列一起删了：**承认这个能力不存在**，将来要做得重写，
而不是留一段读起来像在防着什么的死代码。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from ..infrastructure.database import get_db_session

logger = logging.getLogger(__name__)

# 产品真的会产出的关系类型 —— 全仓唯一的产出方是 ``update_portrait``。
#
# 这不是一张「支持哪些关系」的词表，是一张「谁真的在写」的清单：往里加一个成员，
# 必须同时在源码里加出它的产出方，否则这张清单就描述了一个代码里写不出来、聚合侧
# 永远等不到的关系。此前这里是一张 10 个标签的词表，其中 9 个全仓没有
# 任何一处写得出来，聚合侧为它们准备的分支于是永远不执行 —— 那是「装了没人接线」
# 在图谱层的样子（同形先例：``preset/injector.py`` 里那个因零调用方被删的格式化器）。
_PRODUCED_RELATIONS = frozenset({"has_trait"})

# dimension → 中文分组标题
_DIMENSION_TITLES: Dict[str, str] = {
    "family": "家庭状况",
    "budget_class": "消费水平",
    "travel_style": "旅行风格",
    "lifestyle": "生活方式",
    "personality": "个性特点",
    "constraint": "特殊限制",
}
_DIMENSION_ORDER = list(_DIMENSION_TITLES.keys())


class MemoryGraph:
    """知识图谱读写器。"""

    # -----------------------------------------------------------------------
    # 写入方法
    # -----------------------------------------------------------------------

    async def upsert_entity(
        self,
        user_id: str,
        name: str,
        entity_type: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        创建或更新实体节点。
        基于 (user_id, name, entity_type) 做唯一约束 upsert。
        返回 entity_id。
        """
        import json as _json
        props_json = _json.dumps(properties or {}, ensure_ascii=False)

        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    INSERT INTO memory_entities
                        (user_id, name, entity_type, properties, created_at, updated_at)
                    VALUES
                        (:uid, :name, :etype, CAST(:props AS jsonb), NOW(), NOW())
                    ON CONFLICT (user_id, name, entity_type)
                    DO UPDATE SET
                        properties = EXCLUDED.properties,
                        updated_at = NOW()
                    RETURNING entity_id
                """),
                {"uid": user_id, "name": name, "etype": entity_type, "props": props_json},
            )
            row = result.mappings().first()
            return int(row["entity_id"]) if row else 0

    async def upsert_relation(
        self,
        user_id: str,
        source_name: str,
        source_type: str,
        target_name: str,
        target_type: str,
        relation: str,
        confidence: float = 1.0,
        evidence: str = "",
        session_id: str = "",
        source_properties: Optional[Dict[str, Any]] = None,
        target_properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        创建或更新关系边。
        1. 关系类型必须有产出方，否则当场拒绝（不建实体、不写关系）
        2. upsert 两端实体
        3. 插入新关系（若已存在相同 source/target/relation，更新 confidence）

        写入一条关系**永远不会动到已有的任何一行**：同一个 user 节点下的 trait 天然
        多值并存。这里既没有 UPDATE 也没有 DELETE，是这条规矩的执行处。
        """
        if relation not in _PRODUCED_RELATIONS:
            raise ValueError(
                f"关系类型 {relation!r} 在这个仓里没有产出方；"
                f"图谱只接 {sorted(_PRODUCED_RELATIONS)}，要加新的先把写入方一起加出来"
            )

        source_id = await self.upsert_entity(user_id, source_name, source_type, source_properties)
        target_id = await self.upsert_entity(user_id, target_name, target_type, target_properties)

        if not source_id or not target_id:
            return

        async with get_db_session() as session:
            # 插入或更新同一 (source, target, relation) 的关系
            await session.execute(
                text("""
                    INSERT INTO memory_relations
                        (user_id, source_id, target_id, relation, confidence,
                         evidence, valid_from, source_session, created_at)
                    VALUES
                        (:uid, :src, :tgt, :rel, :conf,
                         :evidence, NOW(), :sess, NOW())
                    ON CONFLICT (user_id, source_id, target_id, relation)
                    DO UPDATE SET
                        confidence = GREATEST(memory_relations.confidence, EXCLUDED.confidence),
                        evidence = CASE
                            WHEN EXCLUDED.evidence <> '' THEN EXCLUDED.evidence
                            ELSE memory_relations.evidence
                        END,
                        source_session = CASE
                            WHEN EXCLUDED.source_session <> '' THEN EXCLUDED.source_session
                            ELSE memory_relations.source_session
                        END
                """),
                {
                    "uid": user_id,
                    "src": source_id,
                    "tgt": target_id,
                    "rel": relation,
                    "conf": confidence,
                    "evidence": evidence,
                    "sess": session_id,
                },
            )

    async def update_portrait(
        self,
        user_id: str,
        portrait_items: List[Dict[str, Any]],
        session_id: str = "",
    ) -> None:
        """
        批量处理 portrait[] 提取结果。
        每个 trait 转为 (用户节点) -[has_trait]-> (trait 实体) 关系。
        trait 实体的 properties 中存储 dimension，供聚合时分组。
        """
        for item in portrait_items:
            trait = (item.get("trait") or "").strip()
            evidence = (item.get("evidence") or "").strip()
            dimension = (item.get("dimension") or "lifestyle").strip()

            if not trait:
                continue

            try:
                await self.upsert_relation(
                    user_id=user_id,
                    source_name="user",
                    source_type="person",
                    target_name=trait,
                    target_type="trait",
                    relation="has_trait",
                    confidence=1.0,
                    evidence=evidence,
                    session_id=session_id,
                    target_properties={"dimension": dimension},
                )
            except Exception as e:
                logger.debug(f"MemoryGraph.update_portrait 单条写入失败: {e}")

    # -----------------------------------------------------------------------
    # 读取方法
    # -----------------------------------------------------------------------

    async def get_relations(self, user_id: str) -> List[Dict[str, Any]]:
        """
        查询用户的全部关系。JOIN entities 取两端的 name / entity_type / properties。

        此前它叫 ``get_active_relations``，多一句 ``WHERE valid_until IS NULL``。
        而唯一给 ``valid_until`` 写过非空值的是一段从未执行的矛盾消解代码 ——
        一个只会过滤出空集的过滤器，和没有过滤器是同一件事，更糟，因为那一行看起来
        像是在防着什么。删除语义是物理删除（``delete_relation`` / ``delete_entity`` /
        ``delete_user_graph``），一行在表里就是有效的，所以名字里的 active 也一并去掉。
        """
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT
                        r.relation_id,
                        r.relation,
                        r.confidence,
                        r.evidence,
                        r.valid_from,
                        se.name        AS source_name,
                        se.entity_type AS source_type,
                        te.name        AS target_name,
                        te.entity_type AS target_type,
                        te.properties  AS target_props
                    FROM memory_relations r
                    JOIN memory_entities se ON r.source_id = se.entity_id
                    JOIN memory_entities te ON r.target_id = te.entity_id
                    WHERE r.user_id = :uid
                    ORDER BY r.valid_from ASC
                """),
                {"uid": user_id},
            )
            rows = result.mappings().fetchall()
        return [dict(r) for r in rows]

    async def delete_user_graph(self, user_id: str) -> Dict[str, int]:
        """Physically delete this user's graph entities and relations."""
        if not user_id:
            return {"entities": 0, "relations": 0}
        async with get_db_session() as session:
            relation_result = await session.execute(
                text("""
                    DELETE FROM memory_relations
                    WHERE user_id = :uid
                    RETURNING relation_id
                """),
                {"uid": user_id},
            )
            entity_result = await session.execute(
                text("""
                    DELETE FROM memory_entities
                    WHERE user_id = :uid
                    RETURNING entity_id
                """),
                {"uid": user_id},
            )
            return {
                "relations": len(relation_result.fetchall()),
                "entities": len(entity_result.fetchall()),
            }

    async def delete_relation(self, user_id: str, relation_id: int | str) -> int:
        """Physically delete one relation owned by the user."""
        if not user_id or relation_id is None:
            return 0
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    DELETE FROM memory_relations
                    WHERE user_id = :uid AND relation_id = :relation_id
                    RETURNING relation_id
                """),
                {"uid": user_id, "relation_id": relation_id},
            )
            return len(result.fetchall())

    async def delete_entity(self, user_id: str, entity_id: int | str) -> Dict[str, int]:
        """Physically delete one entity and its cascaded relations."""
        if not user_id or entity_id is None:
            return {"entities": 0, "relations": 0}
        async with get_db_session() as session:
            relation_result = await session.execute(
                text("""
                    DELETE FROM memory_relations
                    WHERE user_id = :uid AND (source_id = :entity_id OR target_id = :entity_id)
                    RETURNING relation_id
                """),
                {"uid": user_id, "entity_id": entity_id},
            )
            entity_result = await session.execute(
                text("""
                    DELETE FROM memory_entities
                    WHERE user_id = :uid AND entity_id = :entity_id
                    RETURNING entity_id
                """),
                {"uid": user_id, "entity_id": entity_id},
            )
            return {
                "relations": len(relation_result.fetchall()),
                "entities": len(entity_result.fetchall()),
            }

    async def aggregate_portrait(self, user_id: str) -> str:
        """
        从图谱聚合用户画像文本。纯模板，无 LLM 调用。

        输出格式示例：
            家庭状况：已婚，有伴侣出行需求
            消费水平：中产阶级，追求品质但非奢侈
            特殊限制：对海鲜过敏

        Returns:
            画像文本，若无数据则返回空字符串
        """
        relations = await self.get_relations(user_id)
        if not relations:
            return ""

        # 按 dimension 分组收集 trait。
        # 这里只有这一支：图谱里只可能有 ``has_trait``（``upsert_relation`` 是唯一写入口
        # 且当场拒绝其他关系类型）。此前这里还有「去过 / 想去 / 其他偏好」三支，
        # 它们的关系类型全仓没有产出方，一次都没执行过。
        grouped: Dict[str, List[str]] = defaultdict(list)

        for r in relations:
            target_props = r.get("target_props") or {}
            if isinstance(target_props, str):
                import json as _json
                try:
                    target_props = _json.loads(target_props)
                except Exception:
                    target_props = {}

            dimension = target_props.get("dimension", "lifestyle")
            grouped[dimension].append(r["target_name"])

        # 按 dimension 有序输出：同一份图谱两次读取必须给出同一段文本，
        # 所以顺序由 _DIMENSION_ORDER 定，不由录入顺序定。
        lines: List[str] = []
        for dim in _DIMENSION_ORDER:
            traits = grouped.get(dim, [])
            if traits:
                lines.append(f"{_DIMENSION_TITLES[dim]}：{'，'.join(traits)}")

        return "\n".join(lines)

    async def save_portrait_to_profile(self, user_id: str, portrait_text: str) -> None:
        """将已经聚合出的真实画像写入 profile；这是允许创建画像的写路径。"""
        from .user_profile import UserProfileMemory

        profile_memory = UserProfileMemory()
        profile = await profile_memory.ensure_profile_for_write(user_id)
        profile.auto_portrait = portrait_text
        await profile_memory.save_user_profile(profile)

"""数据库行 ↔ 领域对象之间的取值归一。

这些函数原本在九个 store 里各写一份，带着**三种互不相同的空值约定**（空串→None、
空串→空串、None→空串）。同一次响应里因此可以出现两种时间戳形状，而改一处时区处理
要在九个地方找齐。

时区：库里的 `TIMESTAMPTZ` 读回来带 tzinfo，但离线 SQL 与部分驱动会给出 naive 值 ——
一律按 UTC 补齐，绝不让「有没有时区」变成调用方要判的事。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional


def iso_or_none(value: Any) -> Optional[str]:
    """行里的时间戳 → ISO 字符串。空值（含空串）→ None。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.isoformat()
    text = str(value).strip()
    return text or None


def iso_or_empty(value: Any) -> str:
    """同上，但空值 → 空串。给那些字段不可为 None 的领域对象。"""

    return iso_or_none(value) or ""


def json_dumps(value: Any, *, compact: bool = False) -> str:
    """写进 jsonb 列的负载。

    ``default=str``：payload 里带 datetime / Decimal 是常态，少了它那次写入抛
    TypeError，而调用方看到的是「这条命令没落库」。
    """

    separators = (",", ":") if compact else None
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        default=str,
        separators=separators,
    )


def json_object(value: Any) -> Dict[str, Any]:
    """读回来的 jsonb → dict。不是映射就给空 dict，不让调用方判类型。"""

    return dict(value) if isinstance(value, Mapping) else {}

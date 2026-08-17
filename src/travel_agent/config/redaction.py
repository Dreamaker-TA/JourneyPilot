"""配置脱敏：把密钥变成「已设置 / 未设置」，不是变成前四位加星号。

前缀 + 星号看起来更友好，但它把密钥的一部分交出去了 —— 而这份输出会被贴进 issue、
粘进聊天窗口。运营者需要知道的只有一件事：**这台机器上这个 Key 有没有值**。
"""

from __future__ import annotations

from typing import Any, Dict

#: 判据是**字段名**，不是值：一个看起来像普通字符串的 api_key 仍然是密钥，而按值猜
#: 会漏。宁可多脱敏一个无害字段，也不要把一个 Key 写进用户会随手分享的东西里 ——
#: 这一份同时被 `journeypilot config show` 与备份目录里的 config.redacted.yaml 使用。
SECRET_FIELD_MARKERS = ("key", "password", "secret", "token", "credential")


def is_secret_field(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in SECRET_FIELD_MARKERS)


def redact_value(name: str, value: Any) -> Any:
    if not is_secret_field(name):
        return value
    if isinstance(value, str):
        return "<set>" if value.strip() else "<unset>"
    return "<set>" if value else "<unset>"


def redact(payload: Any, *, key: str = "") -> Any:
    """递归脱敏一份已经 dump 成 JSON 值的配置。"""

    if isinstance(payload, dict):
        return {name: redact(item, key=name) for name, item in payload.items()}
    if isinstance(payload, list):
        return [redact(item, key=key) for item in payload]
    return redact_value(key, payload)


def redacted_settings(settings: Any) -> Dict[str, Any]:
    return redact(settings.model_dump(mode="json"))

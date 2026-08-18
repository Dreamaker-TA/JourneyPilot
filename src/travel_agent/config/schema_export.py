"""从 schema 生成配置参考文档与 JSON Schema。

存在的理由是**一致性可以被机械检查**：CI 跑一次生成器，然后要求 git diff 为空。
手写的字段表在第一次改默认值时就落后了，而落后的文档比没有文档更贵 —— 它看起来
是可信的。

分工：字段表、环境变量表、默认值、取值范围**自动生成**；快速开始与解释性文档
手写。不要把整份 README 变成 JSON Schema dump。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple, get_args, get_origin


from .env import env_variable_name, leaf_fields
from .models import CONFIG_VERSION, Settings

GENERATED_HEADER = (
    "<!-- 本文件由 `journeypilot config docs` 生成，不要手改。"
    "字段与默认值改在 src/travel_agent/config/models.py。 -->"
)


def json_schema() -> Dict[str, Any]:
    return Settings.model_json_schema(mode="validation")


def _type_name(annotation: Any) -> str:
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    origin = get_origin(annotation)
    if origin in (list, set, tuple):
        inner = ", ".join(_type_name(arg) for arg in get_args(annotation))
        return f"list[{inner}]" if inner else "list"
    if origin is dict:
        inner = ", ".join(_type_name(arg) for arg in get_args(annotation))
        return f"dict[{inner}]" if inner else "dict"
    if args and len(args) == 1:
        return _type_name(args[0])
    if args:
        return " | ".join(_type_name(arg) for arg in args)
    return getattr(annotation, "__name__", str(annotation))


def _constraint_text(field: Any) -> str:
    """字段声明的取值范围，读 Pydantic 的 metadata 而不是重写一遍。"""

    parts: List[str] = []
    for item in field.metadata:
        for attribute, label in (
            ("ge", ">="),
            ("gt", ">"),
            ("le", "<="),
            ("lt", "<"),
            ("min_length", "长度 >="),
            ("max_length", "长度 <="),
        ):
            value = getattr(item, attribute, None)
            if value is not None:
                parts.append(f"{label} {value}")
    return "，".join(parts)


def _default_text(field: Any) -> str:
    if field.default_factory is not None:
        return "（按段默认）"
    default = field.default
    if default is None:
        return "null"
    if isinstance(default, str):
        return f"`{default}`" if default else "（空）"
    return f"`{default}`"


def field_reference_markdown() -> str:
    """配置字段参考表（路径、类型、默认值、取值范围、环境变量名）。"""

    lines = [
        GENERATED_HEADER,
        "",
        "# 配置字段参考",
        "",
        f"当前 `config_version`: **{CONFIG_VERSION}**",
        "",
        "环境变量一栏为空表示这个字段只能在 `config.yaml` 里配：list / dict 字段",
        "（价格表、MCP server、CORS 来源）不开环境变量入口 —— 用一个字符串表达一张表，",
        "换来的是又一门需要自己的解析器和自己的错误信息的小语言。",
        "",
        "| 字段 | 类型 | 默认值 | 取值范围 | 环境变量 |",
        "|---|---|---|---|---|",
    ]
    env_names = set(_env_capable_paths())
    for path, field in leaf_fields(Settings, include_containers=True).items():
        dotted = _YAML_SPELLING.get(path) or ".".join(path)
        env = env_variable_name(path) if path in env_names else ""
        lines.append(
            f"| `{dotted}` | {_type_name(field.annotation)} | {_default_text(field)} "
            f"| {_constraint_text(field) or '—'} | {f'`{env}`' if env else '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


#: Settings 的内部字段名 → YAML 里真正被接受的写法（见 loader.build_settings）。
#: 照字段名写 `mcp_servers:` 会整体替换掉内置的 11 个 server，而不是合并。
_YAML_SPELLING = {("mcp_servers",): "mcp.servers"}


def _env_capable_paths() -> List[Tuple[str, ...]]:
    from .env import known_env_variables

    return list(known_env_variables(Settings).values())


def json_schema_text() -> str:
    return json.dumps(json_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

"""API 边界上不许再出现 `user_id`：身份由服务端固定，客户端无从声明。

这是 P0-03 的执行点。少了它，一个「顺手加回去的 user_id 参数」会悄悄把
数据作用域重新交回客户端。
"""

from __future__ import annotations

import inspect
import pkgutil
from importlib import import_module

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from pydantic import BaseModel

from travel_agent.api import routes as routes_package
from travel_agent.api import schemas as schemas_module


def _routers() -> list[tuple[str, APIRouter]]:
    found = []
    for info in pkgutil.iter_modules(routes_package.__path__):
        module = import_module(f"{routes_package.__name__}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            found.append((info.name, router))
    return found


@pytest.mark.parametrize("module_name, router", _routers(), ids=lambda v: getattr(v, "prefix", v))
def test_no_route_takes_user_id(module_name: str, router: APIRouter) -> None:
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        assert "{user_id}" not in route.path, f"{module_name}: {route.path} 仍带身份路径段"
        params = route.dependant.path_params + route.dependant.query_params
        names = {param.name for param in params}
        assert "user_id" not in names, f"{module_name}: {route.path} 仍收 user_id 参数"


def test_no_request_or_response_schema_carries_user_id() -> None:
    offenders = [
        name
        for name, obj in vars(schemas_module).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and "user_id" in obj.model_fields
    ]
    assert not offenders, f"这些 schema 仍带 user_id 字段：{offenders}"

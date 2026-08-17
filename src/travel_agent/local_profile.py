"""本地单用户身份的唯一定义处。

JourneyPilot 在一台机器上只为一个人运行，没有登录。`user_id` 因此不是登录身份，
只是库里的数据分区键，取值恒为 `LOCAL_USER_ID`：客户端不再声明自己是谁，
服务端也不接受任何来自请求的身份。
"""

from __future__ import annotations

LOCAL_USER_ID = "local"


def get_local_user_id() -> str:
    """路由取本地身份的唯一入口（FastAPI 依赖）。"""

    return LOCAL_USER_ID

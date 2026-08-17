"""解析出一套**版本匹配**的 `pg_dump` / `pg_restore`。

为什么这件事需要一个模块：本机的客户端版本和服务端版本经常不是一回事。这台开发机上
宿主 `pg_dump` 是 14，Compose 里的服务端是 18 —— 14 的客户端**拒绝** dump 18 的库。
dev docs 02 §5.2 因此要求「使用与数据库 major 版本匹配的客户端」，而不是随手用宿主机
上那个不知道什么版本的。

三种策略，按可靠性排序：

1. `local`：宿主机 `pg_dump` 的 major ≥ 服务端 major；
2. `docker`：服务端跑在容器里 → `docker exec` 进那个容器用它自带的客户端；
3. 都不成立 → **报错**，说清缺什么。不做「先试试看，失败再说」——
   一个坏掉的备份比没有备份更危险，因为它会让人以为自己有备份。

口令一律走 `PGPASSWORD` 环境变量。`docker exec -e PGPASSWORD`（不带 `=value`）
是从客户端环境**透传**，所以口令不会出现在 `docker exec` 的 argv 里、不会被
宿主机上的 `ps` 看到。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .connection import DatabaseTarget

logger = logging.getLogger(__name__)

_VERSION = re.compile(r"\(PostgreSQL\)\s+(\d+)")


class PgToolUnavailable(RuntimeError):
    """找不到版本匹配的客户端。备份/恢复必须在这里停住。"""


@dataclass(frozen=True)
class PgToolRunner:
    """跑 `pg_dump` / `pg_restore` / `psql` 的执行器。"""

    strategy: str  # "local" | "docker"
    client_major: int
    #: strategy == "docker" 时的容器名
    container: str = ""

    def describe(self) -> str:
        if self.strategy == "docker":
            return f"docker exec {self.container}（客户端 PostgreSQL {self.client_major}）"
        return f"宿主机客户端（PostgreSQL {self.client_major}）"

    def _argv(self, tool: str, args: Sequence[str]) -> list[str]:
        if self.strategy == "docker":
            return [
                "docker", "exec", "-i",
                "-e", "PGPASSWORD",
                self.container, tool, *args,
            ]
        return [tool, *args]

    def run(
        self,
        tool: str,
        args: Sequence[str],
        *,
        target: DatabaseTarget,
        stdout_path: Path | None = None,
        stdin_path: Path | None = None,
        timeout: float = 3600.0,
    ) -> subprocess.CompletedProcess:
        """执行一个客户端工具。stdout 可重定向到文件（dump），stdin 可来自文件（restore）。

        **连接参数刻意不含库名之外的主机信息差异**：docker 策略下从容器内部连
        `localhost:5432`，local 策略下连宿主看到的 host:port。同一个 `target`
        两种视角，所以主机参数在这里按策略生成，而不是让调用方猜。
        """

        if self.strategy == "docker":
            connection_args = ["-h", "127.0.0.1", "-p", "5432", "-U", target.user]
        else:
            connection_args = [
                "-h", target.host, "-p", str(target.port), "-U", target.user,
            ]

        env = dict(os.environ)
        env["PGPASSWORD"] = target.password

        argv = self._argv(tool, [*connection_args, *args])
        stdout = stdout_path.open("wb") if stdout_path else subprocess.PIPE
        stdin = stdin_path.open("rb") if stdin_path else None
        try:
            return subprocess.run(  # noqa: S603 — argv 是列表，不过 shell
                argv,
                env=env,
                stdout=stdout,
                stdin=stdin,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        finally:
            if stdout_path and stdout is not subprocess.PIPE:
                stdout.close()
            if stdin is not None:
                stdin.close()


def _local_client_major() -> int | None:
    binary = shutil.which("pg_dump")
    if not binary:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [binary, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION.search(result.stdout or "")
    return int(match.group(1)) if match else None


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def detect_postgres_container(target: DatabaseTarget) -> str:
    """找出把 `target.port` 发布出来的那个容器。找不到返回空串。

    按**发布端口**匹配而不是按容器名或镜像名：名字和镜像都可以被用户改，
    「谁在这个端口上提供服务」才是我们真正要问的问题。
    """

    if not _docker_available():
        return ""
    try:
        result = subprocess.run(  # noqa: S603
            ["docker", "ps", "--format", "{{.Names}}|{{.Ports}}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""

    needle = f":{target.port}->"
    for line in (result.stdout or "").splitlines():
        name, _, ports = line.partition("|")
        if needle in ports:
            return name.strip()
    return ""


def _container_client_major(container: str) -> int | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["docker", "exec", container, "pg_dump", "--version"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION.search(result.stdout or "")
    return int(match.group(1)) if match else None


def resolve_runner(
    target: DatabaseTarget,
    *,
    server_major: int,
    preferred_container: str = "",
) -> PgToolRunner:
    """挑一套 major ≥ `server_major` 的客户端，挑不到就抛 `PgToolUnavailable`。"""

    attempts: list[str] = []

    local_major = _local_client_major()
    if local_major is None:
        attempts.append("宿主机 PATH 里没有 pg_dump")
    elif local_major < server_major:
        attempts.append(
            f"宿主机 pg_dump 是 {local_major}，低于服务端 {server_major}，"
            "它会拒绝导出（不是可以忽略的警告）"
        )
    else:
        return PgToolRunner(strategy="local", client_major=local_major)

    container = preferred_container or detect_postgres_container(target)
    if not container:
        attempts.append(f"没找到发布 {target.port} 端口的 Docker 容器")
    else:
        container_major = _container_client_major(container)
        if container_major is None:
            attempts.append(f"容器 {container} 里跑不动 pg_dump")
        elif container_major < server_major:
            attempts.append(
                f"容器 {container} 的 pg_dump 是 {container_major}，低于服务端 {server_major}"
            )
        else:
            return PgToolRunner(
                strategy="docker", client_major=container_major, container=container
            )

    raise PgToolUnavailable(
        "找不到与服务端版本匹配的 PostgreSQL 客户端，无法生成可信备份：\n  - "
        + "\n  - ".join(attempts)
        + f"\n请安装 postgresql-client-{server_major} 或让数据库跑在可 docker exec 的容器里，"
        "然后重试。"
    )

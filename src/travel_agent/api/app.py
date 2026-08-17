"""
FastAPI 应用工厂 (Serving Layer)
采用 lifespan 模式管理组件生命周期。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from ..builders import AppBuilder, set_components
from ..config import get_settings
from .middleware import request_logging_middleware
from .routes import chat, knowledge, memory, places, preset, product, sessions, system, trip_runs, user

logger = logging.getLogger(__name__)


async def _bootstrap_factory_corpus() -> list[str]:
    """把本地可选的出厂语料种子补进空集合，返回补不上的问题（空 = 没问题）。

    自举**只补空集合**：运营者可以用 `scripts/index_knowledge.py` 往出厂集合里继续加
    东西，开机无条件覆盖会把那些悄悄丢掉。种子本身与判断规则在 `rag/factory_seed.py`。

    **返回的问题不进「请勿放行流量」那一行**，理由与 readiness 里 `data_snapshots`
    那条注释同源：空语料降低的是接地质量，它不让服务答不出话 —— 深度规划的候选身份
    全部来自 Provider（知识库只提名），快问快答还有模型自身知识与实时工具。
    把它塞进那一行会让一个能服务的部署被读成「不许放行」，而那正是
    「一处报警一处放行」的另一个方向。

    但它**必须自己有一条 ERROR**：空库若只剩一句 warning 就等于什么都没做。
    运营者看到的是「系统就绪」旁边一条红线，
    两句话不矛盾 —— 服务能跑，知识库是空的，开演之前去修。
    """

    from ..rag.factory_seed import ensure_factory_corpus

    try:
        report = await ensure_factory_corpus()
    except Exception as exc:  # 自举崩了不拦启动，但要变成一条红线、不是一个静默的坑
        logger.exception("出厂语料自举失败")
        return [f"出厂语料自举失败（{exc}）"]

    if report.loaded:
        logger.info(
            "出厂语料自举：灌入 %s%s",
            "、".join(f"{name} {count} 段" for name, count in sorted(report.loaded.items())),
            "（种子 embedder 与当前配置不符，向量已重算）" if report.reembedded else "",
        )
    elif not report.problems:
        logger.info("出厂语料就位：%s", report.present)
    return list(report.problems)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理：启动时初始化，关闭时清理"""
    settings = get_settings()
    builder = AppBuilder(settings)

    try:
        logger.info("JourneyPilot API 启动中...")
        components = await builder.build()
        set_components(components)
        # 破库 / 无持久化启动绝不能显示「系统就绪」——运营者只看这一行。
        degraded: list[str] = []
        report = components.schema_report
        if report is None or not report.compatible:
            problems = "；".join(report.problems) if report else "合同校验未执行"
            action = (report.next_action if report else "") or "journeypilot doctor"
            degraded.append(f"数据库合同校验未通过（{problems}）→ {action}")
        else:
            # 出厂数据写入只在合同校验通过后跑：结构没建好时这些 DML 会成批失败，
            # 而那批失败只是同一个问题的回声。
            try:
                await components.preset_store.ensure_presets()
                await components.product_configuration_store.ensure_seed()
                logger.info("预设与产品配置初始化完成")
            except Exception as e:
                logger.warning(f"预设初始化失败: {e}")
            # 出厂语料自举。放在这里而不是 run.sh：docker compose 起的那一路不经过
            # run.sh，而「知识库是空的」在两路上是同一件事。空库时检索静默返回 0 条、
            # 规划照常交付，所以要自成一条 ERROR，不并进下面那句「请勿放行流量」，
            # 理由见 `_bootstrap_factory_corpus` 的 docstring。
            corpus_problems = await _bootstrap_factory_corpus()
            if corpus_problems:
                logger.error(
                    "出厂语料未就位，知识库检索会静默返回 0 条"
                    "（详见 GET /api/health/ready 的 knowledge_corpus）| %s",
                    "；".join(corpus_problems),
                )
            # 孤儿 Run 普查放在这里而不是更早：它要写 trip_runs，所以必须等合同校验通过。
            # 在接受业务请求之前跑完 —— 一个显示 running 的 Run 如果在恢复判定之前就被
            # 客户端读到，用户看到的是一个永远不动的进度条。
            recovery = components.run_recovery_service
            if recovery is not None:
                try:
                    recovery_report = await recovery.sweep()
                    recovery_report.log_summary()
                    recovery.start()
                except Exception as e:
                    logger.error("启动恢复普查失败: %s", e, exc_info=True)
                    degraded.append(f"启动恢复普查失败（{e}）")
            # 上一个进程留下的后台任务在这里被重新领走：它们的最终事实在 background_jobs，
            # 不在任何一个已经退出的 Event Loop 里。
            worker = components.background_job_worker
            if worker is not None:
                try:
                    await worker.cleanup()
                except Exception as e:
                    logger.warning("后台任务清理失败: %s", e)
                worker.start()
        # checkpointer 的判据必须与 readiness 逐字同源（system.py::readiness 的
        # `bool(components.checkpointer) or not gates_enabled`）：门关掉时没有
        # checkpointer 是合法配置，不是降级。两处分叉就会一处报警一处放行。
        if not components.checkpointer_available and settings.run_control.plan_gate_enabled:
            degraded.append(
                f"LangGraph checkpointer 不可用（{components.checkpointer_init_error}）"
            )
        if degraded:
            logger.error(
                "系统降级启动，请勿放行流量（详见 GET /api/health/ready）| %s | "
                "工具: %d 个 | 模型: %s",
                "；".join(degraded),
                components.tool_registry.count,
                settings.primary_model.model_name,
            )
        else:
            logger.info(
                f"系统就绪 | 工具: {components.tool_registry.count} 个 | "
                f"模型: {settings.primary_model.model_name}"
            )
        yield
    finally:
        logger.info("JourneyPilot API 关闭中，清理资源...")
        await builder.teardown()


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例"""
    settings = get_settings()

    app = FastAPI(
        title="JourneyPilot TripOps API",
        description="基于 LangGraph、RAG、Memory、Tools 和 TripRun 状态的可信旅行规划 API",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.server.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Gzip 压缩
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # 请求日志
    app.middleware("http")(request_logging_middleware)

    # 注册路由
    app.include_router(chat.router)
    app.include_router(sessions.router)
    app.include_router(system.router)
    app.include_router(knowledge.router)
    app.include_router(user.router)
    app.include_router(places.router)
    app.include_router(memory.router)
    app.include_router(preset.router)
    app.include_router(product.router)
    app.include_router(trip_runs.router)

    # 挂载静态文件（前端构建产物）
    static_path = Path(__file__).parents[4] / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    # 根路径返回 API 说明页面；前端应用由 Vite/static 构建承载。
    @app.get("/")
    async def root():
        from fastapi.responses import HTMLResponse
        return HTMLResponse("""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>JourneyPilot TripOps API</title>
    <style>
        body { font-family: system-ui; max-width: 600px; margin: 80px auto; padding: 20px; }
        h1 { color: #2563eb; }
        code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
        a { color: #2563eb; }
    </style>
</head>
<body>
    <h1>JourneyPilot TripOps API</h1>
    <p>可信 TripOps 后端正在运行：TripRun、Living Itinerary、Evidence、Risk、Memory 与 Tools 均通过显式 API 边界暴露。</p>
    <ul>
        <li><a href="/docs">API 文档 (Swagger)</a></li>
        <li><a href="/api/status">系统状态</a></li>
        <li>聊天接口：<code>POST /api/chat-stream</code></li>
        <li>Trip Run：<code>GET /api/trip-runs</code></li>
        <li>知识库：<code>POST /api/knowledge/index</code></li>
    </ul>
</body>
</html>
        """)

    return app

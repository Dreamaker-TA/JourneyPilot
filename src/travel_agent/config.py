"""
全局配置管理 - 基于 Pydantic Settings
支持 YAML 配置文件 + 环境变量覆盖
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# 简单的 Pydantic BaseModel（非 Settings，不读环境变量）
# 用于 YAML 直接加载
# ---------------------------------------------------------------------------


class FileLogConfig(BaseModel):
    enabled: bool = True
    path: str = "logs/"
    rotation: str = "1 day"
    retention: str = "7 days"


class LoggingConfig(BaseModel):
    level: str = "info"
    file: FileLogConfig = Field(default_factory=FileLogConfig)


# How many tokens one completion may emit.  **One definition site**: the two
# tier configs below and ``api/schemas.ModelConfigRequest`` all read it from
# here, because three independent copies of the same ceiling is how a POST that
# omits the field silently downgrades a running deployment.
#
# ``deepseek/deepseek-v4-flash-0731`` (what this deployment actually runs, via
# OpenRouter) reports ``max_completion_tokens = 65536`` over a 1,048,576-token
# context.
#
# Measured need, from ``run_llm_calls`` over 78 successful ``itinerary_planner``
# completions: min 67, mean 1190, **max 7103**.  32768 is 4.6x the observed
# maximum and still half the provider's own ceiling, so it is picked off the
# measured distribution rather than off the provider's limit — raising it to
# 65536 would buy nothing but a longer runaway.
MAX_COMPLETION_TOKENS = 32768


class PrimaryModelConfig(BaseModel):
    """主力 LLM 配置（用于复杂规划等高推理任务）
    默认: MiniMax-M2.7 @ api.minimaxi.com/v1
    """
    api_key: str = ""
    model_name: str = "MiniMax-M2.7"
    base_url: str = "https://api.minimaxi.com/v1"
    max_tokens: int = MAX_COMPLETION_TOKENS  # 省略该字段的 YAML 不得静默降档
    temperature: float = 0.7
    timeout: int = 120  # LLM 请求超时（秒）；思维模型（MiniMax-M2.7、DeepSeek-R1）复杂推理可能超过 60s


class FastModelConfig(BaseModel):
    """轻量 LLM 配置（用于简单问答、路由判断等低延迟任务）

    方案A（默认）: MiniMax-M2.7 @ api.minimaxi.com/v1
    方案B（可选）: 本地 Qwen，model_name="qwen3.5:4b", base_url="http://localhost:11434/v1"
    """
    api_key: str = ""
    model_name: str = "MiniMax-M2.7"
    base_url: str = "https://api.minimaxi.com/v1"
    max_tokens: int = MAX_COMPLETION_TOKENS  # 与 primary 同顶：fast 截断会产不可解析 JSON
    temperature: float = 0.5
    timeout: int = 30  # Fast 模型超时更短，低延迟场景不应等太久


class EmbeddingConfig(BaseModel):
    """Embedding 模型配置（用于 RAG 向量化）

    provider = "qwen3"  → 本地 Qwen3-Embedding-0.6B ONNX 推理（默认，首次启动从 HF 下载 ~1.2GB）
    provider = "openai" → OpenAI 兼容 Embedding API（需 api_key + base_url）
    provider = "hash"   → 内置确定性哈希向量（零依赖，仅供 RAG 链路跑通，语义质量低）
    """
    provider: str = "qwen3"
    api_key: str = ""
    model_name: str = "n24q02m/Qwen3-Embedding-0.6B-ONNX"
    base_url: str = "https://api.openai.com/v1"
    dimensions: int = 1024


class DatabaseConfig(BaseModel):
    """PostgreSQL + pgvector 数据库配置"""
    host: str = "localhost"
    port: int = 5432
    name: str = "travel_agent"
    user: str = "travel_agent"
    password: str = "travel_agent_pwd"
    pool_size: int = 10
    max_overflow: int = 20

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def sync_url(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class MaintenanceConfig(BaseModel):
    """备份 / 迁移 / 恢复这类维护动作的配置。

    只被 `journeypilot` CLI 与启动编排器读取，API 进程不用它 —— 那条边界是
    ADR-P0-03（API 进程不改 Schema、不改物理数据库）。
    """

    #: 备份根目录。相对路径按仓库根解析（不是当前工作目录 —— 从哪个目录敲命令
    #: 不该改变备份落在哪里）。
    backup_dir: str = "backups"
    #: 自动备份保留份数。手工备份永不自动删除。
    keep_automatic_backups: int = Field(default=5, ge=1)
    #: 跑 pg_dump / pg_restore 的容器名。空 = 按「谁发布了数据库端口」自动探测。
    #: 需要显式指定的场合：一台机器上跑着多个 PostgreSQL 容器共用端口映射。
    postgres_container: str = ""
    #: 迁移锁最长等待秒数。超时报错，绝不无锁继续。
    migration_lock_timeout_seconds: float = Field(default=30.0, gt=0)


class RedisConfig(BaseModel):
    """Redis 缓存配置"""
    host: str = "localhost"
    port: int = 6379
    password: str = ""
    db: int = 0

    @property
    def url(self) -> str:
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class ServerConfig(BaseModel):
    """FastAPI 服务器配置"""
    host: str = "0.0.0.0"
    port: int = 8001
    reload: bool = True
    log_level: str = "info"
    cors_origins: List[str] = Field(
        default_factory=lambda: ["http://localhost:8080", "http://127.0.0.1:8080"]
    )
    allow_runtime_model_config: bool = False


class RAGConfig(BaseModel):
    """RAG 检索配置"""
    chunk_size: int = 500
    chunk_overlap: int = 50
    top_k: int = 5
    score_threshold: float = 0.3
    # 分块策略: "text"（固定窗口）| "semantic"（语义边界）| "contextual"（语义+LLM上下文前缀）
    chunker_type: str = "contextual"
    # 语义分块相关参数
    semantic_split_threshold: float = 0.5   # 相似度低于此值视为语义边界

    # --- 逐项调模型的分块要有界 --------------------------------------------- #
    # semantic / contextual 两档分块的**调用条数由文档大小决定**：逐句 embedding
    # 一次、逐段 LLM 一次。一份合法的 10 MB 上传因此可以排出上万条模型调用，而它们
    # 与全站共用同一条 fast 档 httpx 连接池（``langchain_openai`` 按
    # (base_url, timeout) lru_cache 一个 AsyncClient）——**一个用户的一次上传把所有
    # 人的模型调用排到队尾**，实测服务不可达 7 分半并需重启。
    #
    # 重试与退避**不在这里**：那是 transport 的事（openai SDK 指数退避 +
    # max_retries=2，实测在跑）。这三个数管的是 transport 管不到的那一半 ——
    # 「一次入库允许发多少条、同时几条、失败几条就不再发」。
    #
    # ``model_chunking_max_chars``：超过它的正文一次模型都不调，直接按固定窗口
    # 分块。这个数必须在**花掉任何一次调用之前**可判，所以它的单位是字符而不是段数。
    # 60000 的来处：出厂语料里最大的一篇（travel_tips/voyage-zh-青岛）约 25000 字符
    # / 197 段，取两倍余量；再往上是「入库很慢」换「检索前缀更好」，不划算。
    model_chunking_max_chars: int = Field(default=60_000, ge=1)
    # 同时在飞的逐段 LLM 调用条数上限。它是「上传不许饿死别人」这句话的那个数：
    # fast 档连接池上限 1000，留 4 条给入库、其余留给在线请求。
    contextual_max_concurrency: int = Field(default=4, ge=1)
    # 本篇累计失败多少条就熔断（其余段不再调 LLM，保留原文入库）。上游在限流时
    # 继续发等于替它放大，而一篇资料少几条上下文前缀只是检索差一点。
    contextual_failure_threshold: int = Field(default=8, ge=1)


class RerankConfig(BaseModel):
    """Reranking 精排配置（Step 4: 二阶段精排）

    provider = "cross_encoder" → BGE-Reranker-v2-m3（需要 pip install sentence-transformers）
    provider = "llm"           → 复用现有 LLM 做相关性评分（开箱即用，推荐）
    """
    enabled: bool = True
    provider: str = "llm"            # "cross_encoder" | "llm"
    model_name: str = "BAAI/bge-reranker-v2-m3"
    initial_top_k: int = 20          # 初检候选数量（传给 Hybrid Search 的 top_k）
    final_top_k: int = 5             # 精排后最终返回数量


class DataSnapshotsConfig(BaseModel):
    """Governance thresholds for upstream data committed into the repo.

    ``station_max_age_days``: 90 days is a threshold, not a fact — 12306 publishes
    no version token, so age is the only proxy available and the number expresses
    how long the repo is willing to answer station questions from a table nobody
    has re-checked.  A configuration item rather than a constant because the
    tolerable staleness is an operating decision (a demo week and a quiet month are
    not the same), and because a hardcoded 90 is one more hand-maintained number
    with no way to change it in the field.
    """

    station_max_age_days: int = Field(default=90, gt=0)


class CheckpointRetentionConfig(BaseModel):
    """LangGraph checkpoint retention policy."""

    completed_days: int = 30
    cancelled_days: int = 30
    failed_interrupted_days: int = 90
    batch_size: int = 100
    # When True, AppBuilder refuses to start if Postgres checkpointer init fails.
    require_on_startup: bool = False


class RunControlConfig(BaseModel):
    """JourneyPilot run-control feature flags and execution lease bounds."""

    plan_gate_enabled: bool = True
    #: 租约有效期。过期即视为「执行器不在了」，由恢复扫描接管。
    lease_seconds: int = Field(default=45, gt=0)
    #: 心跳间隔。必须显著小于 lease_seconds，否则一次慢查询就会让活着的 run 被判成孤儿。
    lease_heartbeat_seconds: int = Field(default=10, gt=0)
    #: 连续心跳失败达到这个次数后，执行器停止发起新的外部调用。
    lease_heartbeat_failure_threshold: int = Field(default=3, gt=0)
    #: 孤儿扫描间隔。启动时先扫一次，之后按这个周期复扫 —— 上一个进程死时租约可能还剩
    #: 几十秒，只在启动扫一次会把那些 run 永久留在 running。
    recovery_sweep_seconds: int = Field(default=60, gt=0)
    #: durable command 的轮询间隔。同进程的 API 会立刻唤醒执行器，所以这个值是通知丢失
    #: 或跨进程时的上界延迟，不是正常路径的取消延迟。
    command_poll_seconds: float = Field(default=2.0, gt=0)

    @model_validator(mode="after")
    def _heartbeat_fits_lease(self) -> "RunControlConfig":
        if self.lease_heartbeat_seconds * 2 > self.lease_seconds:
            raise ValueError(
                "lease_heartbeat_seconds 必须小于 lease_seconds 的一半，"
                "否则丢一次心跳就会失去租约"
            )
        return self


class StreamingConfig(BaseModel):
    """SSE 缓冲的内存预算。**全仓唯一定义处**，`api/sse_buffer.py` 读它。"""

    #: 不可丢事件的队列长度。满了生产者等待，不丢。
    critical_queue_size: int = Field(default=128, ge=1)
    #: 相邻同源文本合并成一块的字符上限。太小等于没合并，太大会让首帧变迟。
    max_coalesced_chunk_chars: int = Field(default=2048, ge=1)
    #: 缓冲区里未发出的正文字符总量上限。
    max_pending_text_chars: int = Field(default=65536, ge=1024)
    #: 保活帧间隔。它不进业务队列。
    heartbeat_seconds: float = Field(default=15.0, gt=0)
    #: 生产者等消费者腾位置的上限。超过即判定消费者卡住，结束传输交给 durable 恢复。
    stalled_consumer_seconds: float = Field(default=30.0, gt=0)


class BackgroundJobsConfig(BaseModel):
    """durable 后台任务 worker 的边界。"""

    #: 队列空闲时的轮询间隔。入队方会直接唤醒 worker，所以这是通知丢失时的上界延迟。
    poll_seconds: float = Field(default=5.0, gt=0)
    #: 任务租约。执行中每 1/3 租约续一次；进程崩溃后租约过期，任务重新可领取。
    lease_seconds: int = Field(default=60, gt=0)
    #: 一次领取几条。单机产品不需要并行消费，默认一条一条来。
    batch_size: int = Field(default=1, ge=1)
    #: 已完成任务的保留天数。dead 任务不参与清理，留到用户确认。
    completed_retention_days: int = Field(default=30, ge=1)


class GeocodingConfig(BaseModel):
    """OSM 地点 provider（Nominatim / Overpass）访问配置。"""

    # Nominatim 实例。默认公共实例（零 Key）；自托管实例可经 NOMINATIM_BASE_URL 覆盖以摆脱 1 req/s 限。
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    # Nominatim 合规要求明确标识应用的 User-Agent（禁用 http 库默认 UA）。
    user_agent: str = "JourneyPilot/1.0 (travel itinerary geocoder; https://github.com/journeypilot)"
    # /lookup（批量 osm_id，双语两轮）与 Overpass 的基准超时。这两个端点本来就慢：
    # /lookup 一次要 5 个对象的 addressdetails+namedetails，Overpass 底线另有
    # ``max(30.0, timeout_seconds)`` 兜住。两者都只在身份 fallback 里跑，那条路径
    # 自带 20s 时间片（``_IDENTITY_FALLBACK_BUDGET_SECONDS``），不在首跳同步路径上。
    timeout_seconds: float = Field(default=10.0, gt=0)
    # /search 单独的超时，因为它是「用户敲下目的地」这一跳的同步阻塞点。
    # 本机对公共实例实测健康往返（每次新建 client，含 DNS+TLS）
    # 0.77 / 中位 1.34 / 最慢 2.51 秒，3.4s 覆盖到实测最慢值之上。
    # 上界不是拍的：整条阶梯（速率闸 1.1 + 2 次尝试，见
    # ``nominatim_place_search._PROVIDER_MAX_ATTEMPTS``）要压在 8s 内，
    # 1.1 + 2 × 3.4 = 7.9s，3.4 就是满足这个上界的最大单次超时。
    # 再往上等只是替一个正在 brownout 的公共实例买单，而调用方（intake 路由 /
    # authored 阶梯）宁愿早点拿到一个诚实的失败或换下一条 rung；偶发的一次超时
    # 由第二次尝试兜住，成功后进缓存，同一查询此后不再付这笔钱。
    search_timeout_seconds: float = Field(default=3.4, gt=0)
    # 公共 Nominatim ≤1 req/s；1.1s 留安全裕量（全局速率闸串行保证）。
    min_interval_seconds: float = 1.1
    # 原始 /search 响应的复用窗口。OSM 的城市/车站/景点 relation 不会移动，与既有的
    # ``provider_snapshot_cache.place_identity_ttl_seconds`` 同为 7 天口径。
    search_cache_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, gt=0)
    search_cache_key_prefix: str = Field(
        default="journeypilot:nominatim-search:v1",
        min_length=1,
    )
    search_cache_redis_timeout_seconds: float = Field(default=0.25, gt=0)


class RoutingConfig(BaseModel):
    """Street/public-transit routing provider configuration.

    Two upstreams behind one tool, split by geography rather than by preference:
    amap answers inside the China coordinate box, Transitous/MOTIS answers the
    rest of the world. That is not a taste —     MOTIS carries no mainland-China
    transit feed at all (measured: zero routes visited for a 7.9 km
    Shenzhen hop), and amap covers nowhere else.
    """

    transitous_base_url: str = "https://api.transitous.org"
    amap_base_url: str = "https://restapi.amap.com"
    user_agent: str = "JourneyPilot/2.0 (https://github.com/journeypilot)"
    timeout_seconds: float = 20.0
    min_interval_seconds: float = 1.0


class ProviderSnapshotCacheConfig(BaseModel):
    """Strict Redis policy for reusable, evidence-eligible Provider snapshots."""

    enabled: bool = True
    redis_key_prefix: str = Field(
        default="journeypilot:provider-snapshot:v1",
        min_length=1,
    )
    redis_timeout_seconds: float = Field(default=0.25, gt=0)
    # Low-volatility identity can live longer; route results remain short lived.
    place_identity_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, gt=0)
    route_ttl_seconds: int = Field(default=300, gt=0)



class ToolExposureConfig(BaseModel):
    """Tool Search 按需工具曝光策略。

    默认 ``deferred``：worker agent 不再全量注入工具 schema，只带压缩目录（名称+一行
    描述）进 system prompt，由 ``search_tools`` 元工具按需激活完整 schema。

    - ``mode``: ``deferred`` 走按需曝光；``full`` 一键回退到全量注入（出问题时兜底）。
    - ``worker_only``: 仅对 worker agent（destination/transport/accommodation/itinerary）
      启用；scope/orchestrator 等治理节点不动。
    - ``min_tools_threshold``: 该 agent 可用工具数 ``>=`` 阈值才 defer；阈值以下工具少、
      多一跳 search 得不偿失，自动回落 full。
    """

    mode: str = "deferred"          # "deferred" | "full"
    worker_only: bool = True
    min_tools_threshold: int = 8


class ModelPricingItem(BaseModel):
    """单条模型定价（USD / 1M tokens，读折扣型缓存计费）。

    台账层按 (model_request, provider) 前缀匹配到一条价格，写入时快照计算
    cost_usd。价格随时变动——`effective_from` 记录抓取日期，`source_url` 记录来源，用户
    可在 config.yaml 覆盖或追加档位。
    """

    pattern: str                                   # 模型名前缀（大小写不敏感），如 "MiniMax-M2.7"
    provider: str = ""                             # 可选 provider 约束（infer_provider 输出），空=不限
    input_per_1m: float = 0.0                      # 未命中缓存的输入价
    cached_input_per_1m: Optional[float] = None    # 缓存命中读价（None=按 input 价，无折扣）
    cache_write_per_1m: Optional[float] = None     # 缓存写入价（本卡公式不计，仅登记；写计费型供应商用）
    output_per_1m: float = 0.0                      # 输出价（含 reasoning，不另算）
    currency: str = "USD"
    effective_from: str = ""                        # 价格抓取/生效日期
    source_url: str = ""


class MCPServerItem(BaseModel):
    """单个 MCP 服务器配置"""
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    sse_url: Optional[str] = None
    disabled: bool = False
    description: Optional[str] = None
    required_env: List[str] = Field(default_factory=list)
    # 该 server 两次调用之间的最小间隔。默认 0 = 不限速：绝大多数 server 的配额
    # 是按天算的，间隔闸解决的是「每秒并发超限」这一类拒绝（高德 CUQPS）。
    # 数值必须从供应商控制台现读后写进 config.yaml，**不写死在代码里**——写死就是
    # 又一份会过期的手工数据。不加信号量：MCPManager 的 per-server
    # 锁已经把同一个 server 的调用串起来了，间隔闸只补「相隔多久」这一维。
    min_interval_seconds: float = Field(default=0.0, ge=0)


def _first_env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return ""


def _env_mapping(target_name: str, *aliases: str) -> Dict[str, str]:
    value = _first_env_value(target_name, *aliases)
    return {target_name: value} if value else {}


_REMOVED_MCP_SERVERS = {"google-maps", "tripadvisor"}


def _default_mcp_servers() -> Dict[str, "MCPServerItem"]:
    """
    提供仓库内置的 MCP 默认配置。

    设计原则（真实可用 + 官方可溯源）：
    - 检索层用 agent 原生、带引用的搜索（Tavily/Brave）+ 深度抓取（Firecrawl），
      DuckDuckGo/fetch 作为零 Key 降级兜底。
    - 数据源一律走官方 API（Duffel 航班、高德/百度地图、Open-Meteo），不用浏览器爬虫，
      也不接远程第三方聚合服务。酒店/住宿不依赖专用点评 MCP，统一由
      Tavily/Brave/Firecrawl/fetch 检索覆盖。
    - 地图做高德 + 百度双覆盖；UGC 由公开网页检索补充。

    凡带 required_env 的 server，未配置对应 Key 时会被 MCPManager 自动跳过
    （见 tools/mcp_manager.py::_missing_required_env），不影响其他 server 启动，
    因此可以「配好一个用一个」逐步开通。
    """
    return {
        # ── 检索层：agent 原生搜索 + 深度抓取 ──────────────────────────────
        "tavily-search": MCPServerItem(
            command="npx",
            args=["-y", "tavily-mcp@0.2.21"],
            env=_env_mapping("TAVILY_API_KEY"),
            description="Tavily agent 原生检索（搜索/提取/爬取，返回带引用结果，https://tavily.com）",
            required_env=["TAVILY_API_KEY"],
        ),
        "brave-search": MCPServerItem(
            command="npx",
            args=["-y", "@brave/brave-search-mcp-server@2.1.0"],
            env=_env_mapping("BRAVE_API_KEY"),
            description="Brave 独立索引网络搜索（备份检索源，https://brave.com/search/api）",
            required_env=["BRAVE_API_KEY"],
        ),
        "firecrawl": MCPServerItem(
            command="npx",
            args=["-y", "firecrawl-mcp@3.23.0"],
            env=_env_mapping("FIRECRAWL_API_KEY"),
            description="Firecrawl 深度网页抓取与正文提取（反爬强，https://firecrawl.dev）",
            required_env=["FIRECRAWL_API_KEY"],
        ),
        "duckduckgo-search": MCPServerItem(
            command=sys.executable,
            args=[
                str(Path(__file__).resolve().parents[2] / "mcp_servers" / "search" / "free_search.py")
            ],
            description="DuckDuckGo 免费网络搜索（零 Key，作为降级兜底 free_web_search 的 backbone）",
        ),
        "fetch": MCPServerItem(
            command=sys.executable,
            args=["-m", "mcp_server_fetch"],
            description="通用 HTTP 获取服务（零 Key 轻量抓取）",
        ),
        # ── 地图层：高德 + 百度双覆盖 ──────────────────────────────────────
        "baidu-maps": MCPServerItem(
            command="npx",
            args=["-y", "@baidumap/mcp-server-baidu-map@1.0.5"],
            env=_env_mapping("BAIDU_MAP_API_KEY"),
            description="百度地图（国内）：地理编码、POI、路线、天气、路况（https://lbsyun.baidu.com）",
            required_env=["BAIDU_MAP_API_KEY"],
        ),
        "amap-maps": MCPServerItem(
            command="npx",
            args=["-y", "@amap/amap-maps-mcp-server@0.0.8"],
            env=_env_mapping("AMAP_MAPS_API_KEY"),
            description="高德地图（国内）：地理编码 maps_geo、天气 maps_weather、路线、POI（https://lbs.amap.com）",
            required_env=["AMAP_MAPS_API_KEY"],
        ),
        # ── 航班：仓库内 Duffel v2 MCP（全球，取代已停用的 Amadeus）────────
        # 不再使用 flights-mcp 0.1.0：它固定发送已停用的 Duffel-Version:v1，所有请求均 400。
        # 本地 server 只开放强类型 search_flights，并将 Provider 响应规范化为 long_distance route。
        # 酒店不走这里：Duffel Stays 无干净免费 MCP，住宿改由检索与抓取工具覆盖。
        "duffel-flights": MCPServerItem(
            command=sys.executable,
            args=[
                str(
                    Path(__file__).resolve().parents[2]
                    / "mcp_servers"
                    / "flights"
                    / "duffel_mcp.py"
                )
            ],
            env=_env_mapping("DUFFEL_API_KEY_LIVE"),
            description="Duffel v2 航班搜索（全球官方 API，https://duffel.com）",
            required_env=["DUFFEL_API_KEY_LIVE"],
        ),
        # ── 火车：仓库内 12306 MCP（国内，零 Key）──────────────────────────
        # 不再使用 12306-mcp 0.3.9（已是最新发布版）：它把 12306 早已下线的
        # /otn/leftTicket/query 写死在代码里，get-tickets 恒定 404，交付出的国内
        # 行程一条城际铁路腿都没有。12306 会轮换该路径并把当前值发布在
        # /otn/leftTicket/init 的 HTML 里（CLeftTicketUrl），只有仓库内客户端能
        # 跟着轮换走；官方站点表快照见 mcp_servers/rail/station_name.js。
        "12306-train": MCPServerItem(
            command=sys.executable,
            args=[
                str(
                    Path(__file__).resolve().parents[2]
                    / "mcp_servers"
                    / "rail"
                    / "rail_12306_mcp.py"
                )
            ],
            description="12306 火车票与车次查询服务（仓库内实现，零 Key，国内）",
        ),
        # ── 天气：独立一等源（全球，零 Key）────────────────────────────────
        # 注：mcp-openweathermap 的 fastmcp 版本存在 completion 能力声明 bug，
        # 与本项目 MCP 客户端不兼容（启动即崩），故改用零 Key 的 Open-Meteo。
        "open-meteo": MCPServerItem(
            command="npx",
            args=["-y", "open-meteo-mcp@0.1.0"],
            description="Open-Meteo MCP：当前天气、固定 7 日预报、历史天气与空气质量（零 Key，https://open-meteo.com）",
        ),
        # ── 汇率（全球，零 Key）────────────────────────────────────────────
        "currency-exchange-mcp": MCPServerItem(
            command=sys.executable,
            args=[
                str(
                    Path(__file__).resolve().parents[2]
                    / "mcp_servers"
                    / "currency"
                    / "frankfurter_mcp.py"
                )
            ],
            description="实时汇率换算（内置 Frankfurter API，无需 API Key，不依赖 Node）",
        ),
    }


def _default_model_pricing() -> List["ModelPricingItem"]:
    """内置定价快照（USD / 1M tokens，国际站口径）。

    分层定价（Qwen）取基础档；价格随时变动，用户可在
    config.yaml 的 `model_pricing` 覆盖。前缀匹配按 pattern 最长优先（见 resolve_price）。
    """
    ds = "https://api-docs.deepseek.com/quick_start/pricing"
    orouter = "https://openrouter.ai/api/v1/models"
    qwen = "https://www.alibabacloud.com/help/en/model-studio/model-pricing"
    mm = "https://platform.minimax.io/docs/guides/pricing-paygo"
    oai = "https://developers.openai.com/api/docs/pricing"
    day = "2026-07-03"
    return [
        # MiniMax（M2.7 有显式缓存写入费；本卡读折扣公式不计 write，仅登记）
        ModelPricingItem(pattern="MiniMax-M2.7", provider="minimax", input_per_1m=0.30,
                         cached_input_per_1m=0.06, cache_write_per_1m=0.375, output_per_1m=1.20,
                         effective_from=day, source_url=mm),
        ModelPricingItem(pattern="MiniMax-M3", provider="minimax", input_per_1m=0.30,
                         cached_input_per_1m=0.06, output_per_1m=1.20,
                         effective_from=day, source_url=mm),
        # DeepSeek（缓存命中价≈未命中 1/50）
        ModelPricingItem(pattern="deepseek-v4-flash", provider="deepseek", input_per_1m=0.14,
                         cached_input_per_1m=0.0028, output_per_1m=0.28,
                         effective_from=day, source_url=ds),
        ModelPricingItem(pattern="deepseek-v4-pro", provider="deepseek", input_per_1m=0.435,
                         cached_input_per_1m=0.003625, output_per_1m=0.87,
                         effective_from=day, source_url=ds),
        # The same two models reached through OpenRouter.  A separate pattern
        # rather than a looser one: the model id carries the `deepseek/` prefix
        # there, so prefix matching never reaches the direct entries above, and
        # the cache-read price genuinely differs (OpenRouter lists 1/5 of the
        # miss price for flash where DeepSeek direct lists 1/50).  Without these
        # rows `resolve_price` returns None and every cost read-out is null.
        ModelPricingItem(pattern="deepseek/deepseek-v4-flash", provider="deepseek",
                         input_per_1m=0.14, cached_input_per_1m=0.028, output_per_1m=0.28,
                         effective_from="2026-07-31", source_url=orouter),
        ModelPricingItem(pattern="deepseek/deepseek-v4-pro", provider="deepseek",
                         input_per_1m=0.435, cached_input_per_1m=0.003625, output_per_1m=0.87,
                         effective_from="2026-07-31", source_url=orouter),
        # Qwen / 阿里云（分层定价取基础档；隐式缓存命中 20%）
        ModelPricingItem(pattern="Qwen-Flash", provider="qwen", input_per_1m=0.05,
                         cached_input_per_1m=0.01, output_per_1m=0.40,
                         effective_from=day, source_url=qwen),
        ModelPricingItem(pattern="Qwen3.7-Plus", provider="qwen", input_per_1m=0.40,
                         cached_input_per_1m=0.08, output_per_1m=1.60,
                         effective_from=day, source_url=qwen),
        # OpenAI 5.4 系
        ModelPricingItem(pattern="gpt-5.4-nano", provider="openai", input_per_1m=0.20,
                         cached_input_per_1m=0.02, output_per_1m=1.25,
                         effective_from=day, source_url=oai),
        ModelPricingItem(pattern="gpt-5.4-mini", provider="openai", input_per_1m=0.75,
                         cached_input_per_1m=0.075, output_per_1m=4.50,
                         effective_from=day, source_url=oai),
        ModelPricingItem(pattern="gpt-5.4", provider="openai", input_per_1m=2.50,
                         cached_input_per_1m=0.25, output_per_1m=15.00,
                         effective_from=day, source_url=oai),
    ]


# ---------------------------------------------------------------------------
# 根配置对象（从 YAML + 环境变量合并后的最终配置）
# ---------------------------------------------------------------------------

class Settings(BaseModel):
    """
    全局设置对象，通过 get_settings() 获取单例。
    优先级：环境变量 > config.yaml > 默认值
    """
    # 环境标识
    env: str = "development"
    debug: bool = False

    # 子配置
    primary_model: PrimaryModelConfig = Field(default_factory=PrimaryModelConfig)
    fast_model: FastModelConfig = Field(default_factory=FastModelConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    checkpoint_retention: CheckpointRetentionConfig = Field(default_factory=CheckpointRetentionConfig)
    data_snapshots: DataSnapshotsConfig = Field(default_factory=DataSnapshotsConfig)
    run_control: RunControlConfig = Field(default_factory=RunControlConfig)
    background_jobs: BackgroundJobsConfig = Field(default_factory=BackgroundJobsConfig)
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)
    geocoding: GeocodingConfig = Field(default_factory=GeocodingConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    provider_snapshot_cache: ProviderSnapshotCacheConfig = Field(default_factory=ProviderSnapshotCacheConfig)
    tool_exposure: ToolExposureConfig = Field(default_factory=ToolExposureConfig)
    model_pricing: List[ModelPricingItem] = Field(default_factory=_default_model_pricing)
    mcp_servers: Dict[str, MCPServerItem] = Field(default_factory=_default_mcp_servers)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# ---------------------------------------------------------------------------
# YAML 配置加载逻辑
# ---------------------------------------------------------------------------

def _find_config_yaml() -> Optional[Path]:
    """
    查找 config.yaml，优先级：
    1. 环境变量 CONFIG_PATH 指定的路径
    2. 从本文件 (__file__) 向上遍历目录树直到找到 config.yaml
    3. 当前工作目录（兜底）
    """
    # 1. 环境变量显式指定
    env_path = os.getenv("CONFIG_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    # 2. 从 config.py 所在位置向上查找项目根
    current = Path(__file__).resolve().parent
    for _ in range(10):  # 最多向上 10 层
        candidate = current / "config.yaml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent

    # 3. 兜底：当前工作目录
    candidate = Path("config.yaml")
    return candidate if candidate.exists() else None


def _load_yaml_config(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _apply_env_overrides(settings: Settings) -> Settings:
    """从环境变量覆盖关键配置"""
    # 数据库
    env_db_host = os.getenv("DB_HOST")
    if env_db_host:
        settings.database.host = env_db_host
    env_db_port = os.getenv("DB_PORT")
    if env_db_port:
        settings.database.port = int(env_db_port)
    env_db_name = os.getenv("DB_NAME")
    if env_db_name:
        settings.database.name = env_db_name
    env_db_user = os.getenv("DB_USER")
    if env_db_user:
        settings.database.user = env_db_user
    env_db_password = os.getenv("DB_PASSWORD")
    if env_db_password:
        settings.database.password = env_db_password

    # Redis
    env_redis_host = os.getenv("REDIS_HOST")
    if env_redis_host:
        settings.redis.host = env_redis_host
    env_redis_port = os.getenv("REDIS_PORT")
    if env_redis_port:
        settings.redis.port = int(env_redis_port)
    env_redis_password = os.getenv("REDIS_PASSWORD")
    if env_redis_password:
        settings.redis.password = env_redis_password

    # 服务器
    env_server_port = os.getenv("SERVER_PORT")
    if env_server_port:
        settings.server.port = int(env_server_port)
    env_server_host = os.getenv("SERVER_HOST")
    if env_server_host:
        settings.server.host = env_server_host
    env_server_reload = os.getenv("SERVER_RELOAD")
    if env_server_reload is not None:
        settings.server.reload = env_server_reload.strip().lower() in {"1", "true", "yes", "on"}
    env_runtime_config = os.getenv("ALLOW_RUNTIME_MODEL_CONFIG")
    if env_runtime_config is not None:
        settings.server.allow_runtime_model_config = env_runtime_config.strip().lower() in {"1", "true", "yes", "on"}

    # Primary model API key
    val = os.getenv("PRIMARY_API_KEY")
    if val:
        settings.primary_model.api_key = val

    env_model_name = os.getenv("PRIMARY_MODEL_NAME")
    if env_model_name:
        settings.primary_model.model_name = env_model_name

    env_base_url = os.getenv("PRIMARY_BASE_URL")
    if env_base_url:
        settings.primary_model.base_url = env_base_url

    # Fast model overrides. Keep explicit fast tier configurable without editing YAML
    # when a deployment injects secrets exclusively through environment variables.
    val = os.getenv("FAST_API_KEY")
    if val:
        settings.fast_model.api_key = val
    env_fast_model_name = os.getenv("FAST_MODEL_NAME")
    if env_fast_model_name:
        settings.fast_model.model_name = env_fast_model_name
    env_fast_base_url = os.getenv("FAST_BASE_URL")
    if env_fast_base_url:
        settings.fast_model.base_url = env_fast_base_url

    # Embedding overrides for containerized smoke/production deployments.
    env_embedding_provider = os.getenv("EMBEDDING_PROVIDER")
    if env_embedding_provider:
        settings.embedding.provider = env_embedding_provider
    env_embedding_model = os.getenv("EMBEDDING_MODEL_NAME")
    if env_embedding_model:
        settings.embedding.model_name = env_embedding_model
    env_embedding_base_url = os.getenv("EMBEDDING_BASE_URL")
    if env_embedding_base_url:
        settings.embedding.base_url = env_embedding_base_url
    env_embedding_api_key = os.getenv("EMBEDDING_API_KEY")
    if env_embedding_api_key:
        settings.embedding.api_key = env_embedding_api_key
    env_embedding_dimensions = os.getenv("EMBEDDING_DIMENSIONS")
    if env_embedding_dimensions:
        settings.embedding.dimensions = int(env_embedding_dimensions)

    env_app_env = os.getenv("APP_ENV")
    if env_app_env:
        settings.env = env_app_env


    # Geocoding：自托管 Nominatim 实例（摆脱公共实例 1 req/s 限）
    env_nominatim = os.getenv("NOMINATIM_BASE_URL")
    if env_nominatim:
        settings.geocoding.nominatim_base_url = env_nominatim

    env_transitous = os.getenv("TRANSITOUS_BASE_URL")
    if env_transitous:
        settings.routing.transitous_base_url = env_transitous


    # MCP 环境变量按 required_env 原名读取。
    for server in settings.mcp_servers.values():
        merged_env = dict(server.env or {})
        for key in server.required_env:
            value = _first_env_value(key)
            if value:
                merged_env[key] = value
        server.env = merged_env or None

    return settings


def _build_settings_from_yaml(data: Dict[str, Any]) -> Settings:
    """将 YAML dict 构造为 Settings"""

    settings = Settings(
        env=data.get("env", "development"),
        debug=data.get("debug", False),
    )

    # primary_model（兼容旧版 "model" 字段）
    pm_data = data.get("primary_model", data.get("model", {}))
    if pm_data:
        settings.primary_model = PrimaryModelConfig(**pm_data)

    fm_data = data.get("fast_model", {})
    if fm_data:
        settings.fast_model = FastModelConfig(**fm_data)

    em_data = data.get("embedding", {})
    if em_data:
        settings.embedding = EmbeddingConfig(**em_data)

    db_data = data.get("database", {})
    if db_data:
        settings.database = DatabaseConfig(**db_data)

    mt_data = data.get("maintenance", {})
    if mt_data:
        settings.maintenance = MaintenanceConfig(**mt_data)

    rd_data = data.get("redis", {})
    if rd_data:
        settings.redis = RedisConfig(**rd_data)

    sv_data = data.get("server", {})
    if sv_data:
        settings.server = ServerConfig(**sv_data)

    rg_data = data.get("rag", {})
    if rg_data:
        settings.rag = RAGConfig(**rg_data)

    rk_data = data.get("rerank", {})
    if rk_data:
        settings.rerank = RerankConfig(**rk_data)

    cr_data = data.get("checkpoint_retention", {})
    if cr_data:
        settings.checkpoint_retention = CheckpointRetentionConfig(**cr_data)

    rc_data = data.get("run_control", {})
    if rc_data:
        settings.run_control = RunControlConfig(**rc_data)

    gc_data = data.get("geocoding", {})
    if gc_data:
        settings.geocoding = GeocodingConfig(**gc_data)

    routing_data = data.get("routing", {})
    if routing_data:
        settings.routing = RoutingConfig(**routing_data)

    snapshot_cache_data = data.get("provider_snapshot_cache", {})
    if snapshot_cache_data:
        settings.provider_snapshot_cache = ProviderSnapshotCacheConfig(**snapshot_cache_data)

    te_data = data.get("tool_exposure", {})
    if te_data:
        settings.tool_exposure = ToolExposureConfig(**te_data)

    data_snapshots_data = data.get("data_snapshots", {})
    if data_snapshots_data:
        settings.data_snapshots = DataSnapshotsConfig(**data_snapshots_data)

    # model_pricing 为整体替换语义（用户给了就用用户的，未给则用内置快照）。
    mp_data = data.get("model_pricing")
    if mp_data:
        settings.model_pricing = [ModelPricingItem(**item) for item in mp_data]

    # logging
    log_data = data.get("logging", {})
    if log_data:
        file_data = log_data.get("file", {})
        settings.logging = LoggingConfig(
            level=log_data.get("level", "info"),
            file=FileLogConfig(**file_data) if file_data else FileLogConfig(),
        )

    # MCP servers
    # 将 YAML 中的 server 配置合并到默认值，而非完全替换。
    # 这样用户只需在 config.yaml 里填写 env 字段，required_env/command/args
    # 等内置默认值会自动保留，避免健康检查因 required_env=[] 而漏报配置缺失。
    mcp_data = data.get("mcp", {}).get("servers", {})
    for name, cfg in mcp_data.items():
        if name in _REMOVED_MCP_SERVERS:
            continue
        if name in settings.mcp_servers:
            base = settings.mcp_servers[name].model_dump()
            # 仅覆盖 YAML 中显式设置的非 None 字段
            base.update({k: v for k, v in cfg.items() if v is not None})
            settings.mcp_servers[name] = MCPServerItem(**base)
        else:
            settings.mcp_servers[name] = MCPServerItem(**cfg)

    return settings


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """获取全局配置单例"""
    global _settings
    if _settings is None:
        yaml_path = _find_config_yaml()
        if yaml_path:
            data = _load_yaml_config(yaml_path)
            _settings = _build_settings_from_yaml(data)
        else:
            _settings = Settings()
        # 环境变量覆盖
        _settings = _apply_env_overrides(_settings)
    return _settings


def reload_settings() -> Settings:
    """重新加载配置（用于热更新）"""
    global _settings
    _settings = None
    return get_settings()


def resolve_price(
    model: Optional[str],
    provider: Optional[str] = None,
    *,
    pricing: Optional[List[ModelPricingItem]] = None,
) -> Optional[ModelPricingItem]:
    """按 (model, provider) 前缀匹配到一条价格；未命中返回 None（→ 只报 token 不编造成本）。

    - 大小写不敏感的前缀匹配：``model`` 以 ``item.pattern`` 开头即候选。
    - ``item.provider`` 非空时必须与传入 ``provider`` 相等（provider 缺省则不约束）。
    - 多条命中取 **pattern 最长** 者（最具体的档位优先，如 gpt-5.4-mini 胜过 gpt-5.4）。
    """
    if not model:
        return None
    table = pricing if pricing is not None else get_settings().model_pricing
    model_lc = model.lower()
    provider_lc = (provider or "").lower()
    best: Optional[ModelPricingItem] = None
    for item in table:
        pattern = (item.pattern or "").lower()
        if not pattern or not model_lc.startswith(pattern):
            continue
        if item.provider and provider_lc and item.provider.lower() != provider_lc:
            continue
        if best is None or len(item.pattern) > len(best.pattern):
            best = item
    return best


def persist_model_config(tier: str, model_data: Dict[str, Any]) -> None:
    """将模型配置持久化到 config.yaml"""
    yaml_path = _find_config_yaml()
    if not yaml_path:
        raise FileNotFoundError("config.yaml 未找到，无法持久化配置")
    data = _load_yaml_config(yaml_path)
    key = "primary_model" if tier == "primary" else "fast_model"
    if key not in data:
        data[key] = {}
    data[key].update(model_data)
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

<!-- 本文件由 `journeypilot config docs` 生成，不要手改。字段与默认值改在 src/travel_agent/config/models.py。 -->

# 配置字段参考

当前 `config_version`: **2**

环境变量一栏为空表示这个字段只能在 `config.yaml` 里配：list / dict 字段
（价格表、MCP server、CORS 来源）不开环境变量入口 —— 用一个字符串表达一张表，
换来的是又一门需要自己的解析器和自己的错误信息的小语言。

| 字段 | 类型 | 默认值 | 取值范围 | 环境变量 |
|---|---|---|---|---|
| `env` | str | `development` | — | `JOURNEYPILOT_ENV` |
| `debug` | bool | `False` | — | `JOURNEYPILOT_DEBUG` |
| `primary_model.api_key` | str | （空） | — | `JOURNEYPILOT_PRIMARY_MODEL__API_KEY` |
| `primary_model.model_name` | str | `MiniMax-M2.7` | — | `JOURNEYPILOT_PRIMARY_MODEL__MODEL_NAME` |
| `primary_model.base_url` | str | `https://api.minimaxi.com/v1` | — | `JOURNEYPILOT_PRIMARY_MODEL__BASE_URL` |
| `primary_model.max_tokens` | int | `32768` | — | `JOURNEYPILOT_PRIMARY_MODEL__MAX_TOKENS` |
| `primary_model.temperature` | float | `0.7` | — | `JOURNEYPILOT_PRIMARY_MODEL__TEMPERATURE` |
| `primary_model.timeout` | int | `120` | — | `JOURNEYPILOT_PRIMARY_MODEL__TIMEOUT` |
| `fast_model.api_key` | str | （空） | — | `JOURNEYPILOT_FAST_MODEL__API_KEY` |
| `fast_model.model_name` | str | `MiniMax-M2.7` | — | `JOURNEYPILOT_FAST_MODEL__MODEL_NAME` |
| `fast_model.base_url` | str | `https://api.minimaxi.com/v1` | — | `JOURNEYPILOT_FAST_MODEL__BASE_URL` |
| `fast_model.max_tokens` | int | `32768` | — | `JOURNEYPILOT_FAST_MODEL__MAX_TOKENS` |
| `fast_model.temperature` | float | `0.5` | — | `JOURNEYPILOT_FAST_MODEL__TEMPERATURE` |
| `fast_model.timeout` | int | `30` | — | `JOURNEYPILOT_FAST_MODEL__TIMEOUT` |
| `embedding.provider` | str | `qwen3` | — | `JOURNEYPILOT_EMBEDDING__PROVIDER` |
| `embedding.api_key` | str | （空） | — | `JOURNEYPILOT_EMBEDDING__API_KEY` |
| `embedding.model_name` | str | `n24q02m/Qwen3-Embedding-0.6B-ONNX` | — | `JOURNEYPILOT_EMBEDDING__MODEL_NAME` |
| `embedding.base_url` | str | `https://api.openai.com/v1` | — | `JOURNEYPILOT_EMBEDDING__BASE_URL` |
| `embedding.dimensions` | int | `1024` | — | `JOURNEYPILOT_EMBEDDING__DIMENSIONS` |
| `database.host` | str | `localhost` | — | `JOURNEYPILOT_DATABASE__HOST` |
| `database.port` | int | `5432` | — | `JOURNEYPILOT_DATABASE__PORT` |
| `database.name` | str | `travel_agent` | — | `JOURNEYPILOT_DATABASE__NAME` |
| `database.user` | str | `travel_agent` | — | `JOURNEYPILOT_DATABASE__USER` |
| `database.password` | str | `travel_agent_pwd` | — | `JOURNEYPILOT_DATABASE__PASSWORD` |
| `database.pool_size` | int | `10` | — | `JOURNEYPILOT_DATABASE__POOL_SIZE` |
| `database.max_overflow` | int | `20` | — | `JOURNEYPILOT_DATABASE__MAX_OVERFLOW` |
| `maintenance.backup_dir` | str | `backups` | — | `JOURNEYPILOT_MAINTENANCE__BACKUP_DIR` |
| `maintenance.keep_automatic_backups` | int | `5` | >= 1 | `JOURNEYPILOT_MAINTENANCE__KEEP_AUTOMATIC_BACKUPS` |
| `maintenance.postgres_container` | str | （空） | — | `JOURNEYPILOT_MAINTENANCE__POSTGRES_CONTAINER` |
| `maintenance.migration_lock_timeout_seconds` | float | `30.0` | > 0 | `JOURNEYPILOT_MAINTENANCE__MIGRATION_LOCK_TIMEOUT_SECONDS` |
| `redis.host` | str | `localhost` | — | `JOURNEYPILOT_REDIS__HOST` |
| `redis.port` | int | `6379` | — | `JOURNEYPILOT_REDIS__PORT` |
| `redis.password` | str | （空） | — | `JOURNEYPILOT_REDIS__PASSWORD` |
| `redis.db` | int | `0` | — | `JOURNEYPILOT_REDIS__DB` |
| `server.host` | str | `0.0.0.0` | — | `JOURNEYPILOT_SERVER__HOST` |
| `server.port` | int | `8001` | — | `JOURNEYPILOT_SERVER__PORT` |
| `server.reload` | bool | `True` | — | `JOURNEYPILOT_SERVER__RELOAD` |
| `server.log_level` | str | `info` | — | `JOURNEYPILOT_SERVER__LOG_LEVEL` |
| `server.cors_origins` | list[str] | （按段默认） | — | — |
| `server.allow_runtime_model_config` | bool | `False` | — | `JOURNEYPILOT_SERVER__ALLOW_RUNTIME_MODEL_CONFIG` |
| `rag.chunk_size` | int | `500` | — | `JOURNEYPILOT_RAG__CHUNK_SIZE` |
| `rag.chunk_overlap` | int | `50` | — | `JOURNEYPILOT_RAG__CHUNK_OVERLAP` |
| `rag.top_k` | int | `5` | — | `JOURNEYPILOT_RAG__TOP_K` |
| `rag.score_threshold` | float | `0.3` | — | `JOURNEYPILOT_RAG__SCORE_THRESHOLD` |
| `rag.chunker_type` | str | `contextual` | — | `JOURNEYPILOT_RAG__CHUNKER_TYPE` |
| `rag.semantic_split_threshold` | float | `0.5` | — | `JOURNEYPILOT_RAG__SEMANTIC_SPLIT_THRESHOLD` |
| `rag.model_chunking_max_chars` | int | `60000` | >= 1 | `JOURNEYPILOT_RAG__MODEL_CHUNKING_MAX_CHARS` |
| `rag.contextual_failure_threshold` | int | `8` | >= 1 | `JOURNEYPILOT_RAG__CONTEXTUAL_FAILURE_THRESHOLD` |
| `rerank.enabled` | bool | `True` | — | `JOURNEYPILOT_RERANK__ENABLED` |
| `rerank.provider` | str | `llm` | — | `JOURNEYPILOT_RERANK__PROVIDER` |
| `rerank.model_name` | str | `BAAI/bge-reranker-v2-m3` | — | `JOURNEYPILOT_RERANK__MODEL_NAME` |
| `rerank.initial_top_k` | int | `20` | — | `JOURNEYPILOT_RERANK__INITIAL_TOP_K` |
| `rerank.final_top_k` | int | `5` | — | `JOURNEYPILOT_RERANK__FINAL_TOP_K` |
| `checkpoint_retention.completed_days` | int | `30` | — | `JOURNEYPILOT_CHECKPOINT_RETENTION__COMPLETED_DAYS` |
| `checkpoint_retention.cancelled_days` | int | `30` | — | `JOURNEYPILOT_CHECKPOINT_RETENTION__CANCELLED_DAYS` |
| `checkpoint_retention.failed_interrupted_days` | int | `90` | — | `JOURNEYPILOT_CHECKPOINT_RETENTION__FAILED_INTERRUPTED_DAYS` |
| `checkpoint_retention.batch_size` | int | `100` | — | `JOURNEYPILOT_CHECKPOINT_RETENTION__BATCH_SIZE` |
| `checkpoint_retention.require_on_startup` | bool | `False` | — | `JOURNEYPILOT_CHECKPOINT_RETENTION__REQUIRE_ON_STARTUP` |
| `data_snapshots.station_max_age_days` | int | `90` | > 0 | `JOURNEYPILOT_DATA_SNAPSHOTS__STATION_MAX_AGE_DAYS` |
| `run_control.plan_gate_enabled` | bool | `True` | — | `JOURNEYPILOT_RUN_CONTROL__PLAN_GATE_ENABLED` |
| `run_control.lease_seconds` | int | `45` | > 0 | `JOURNEYPILOT_RUN_CONTROL__LEASE_SECONDS` |
| `run_control.lease_heartbeat_seconds` | int | `10` | > 0 | `JOURNEYPILOT_RUN_CONTROL__LEASE_HEARTBEAT_SECONDS` |
| `run_control.lease_heartbeat_failure_threshold` | int | `3` | > 0 | `JOURNEYPILOT_RUN_CONTROL__LEASE_HEARTBEAT_FAILURE_THRESHOLD` |
| `run_control.recovery_sweep_seconds` | int | `60` | > 0 | `JOURNEYPILOT_RUN_CONTROL__RECOVERY_SWEEP_SECONDS` |
| `run_control.command_poll_seconds` | float | `2.0` | > 0 | `JOURNEYPILOT_RUN_CONTROL__COMMAND_POLL_SECONDS` |
| `run_deadline.target_seconds` | int | `375` | > 0 | `JOURNEYPILOT_RUN_DEADLINE__TARGET_SECONDS` |
| `run_deadline.closeout_seconds` | int | `450` | > 0 | `JOURNEYPILOT_RUN_DEADLINE__CLOSEOUT_SECONDS` |
| `run_deadline.composition_seconds` | int | `570` | > 0 | `JOURNEYPILOT_RUN_DEADLINE__COMPOSITION_SECONDS` |
| `run_deadline.delivery_seconds` | int | `600` | > 0 | `JOURNEYPILOT_RUN_DEADLINE__DELIVERY_SECONDS` |
| `run_budget.max_llm_calls` | int | `100` | >= 1 | `JOURNEYPILOT_RUN_BUDGET__MAX_LLM_CALLS` |
| `run_budget.max_tool_calls` | int | `150` | >= 1 | `JOURNEYPILOT_RUN_BUDGET__MAX_TOOL_CALLS` |
| `run_budget.max_input_tokens` | int | `1000000` | >= 1 | `JOURNEYPILOT_RUN_BUDGET__MAX_INPUT_TOKENS` |
| `run_budget.max_output_tokens` | int | `100000` | >= 1 | `JOURNEYPILOT_RUN_BUDGET__MAX_OUTPUT_TOKENS` |
| `run_budget.max_cost_usd` | float | `5.0` | > 0 | `JOURNEYPILOT_RUN_BUDGET__MAX_COST_USD` |
| `run_budget.max_tool_retries_per_target` | int | `2` | >= 0 | `JOURNEYPILOT_RUN_BUDGET__MAX_TOOL_RETRIES_PER_TARGET` |
| `background_jobs.poll_seconds` | float | `5.0` | > 0 | `JOURNEYPILOT_BACKGROUND_JOBS__POLL_SECONDS` |
| `background_jobs.lease_seconds` | int | `60` | > 0 | `JOURNEYPILOT_BACKGROUND_JOBS__LEASE_SECONDS` |
| `background_jobs.batch_size` | int | `1` | >= 1 | `JOURNEYPILOT_BACKGROUND_JOBS__BATCH_SIZE` |
| `background_jobs.completed_retention_days` | int | `30` | >= 1 | `JOURNEYPILOT_BACKGROUND_JOBS__COMPLETED_RETENTION_DAYS` |
| `streaming.critical_queue_size` | int | `128` | >= 1 | `JOURNEYPILOT_STREAMING__CRITICAL_QUEUE_SIZE` |
| `streaming.max_coalesced_chunk_chars` | int | `2048` | >= 1 | `JOURNEYPILOT_STREAMING__MAX_COALESCED_CHUNK_CHARS` |
| `streaming.max_pending_text_chars` | int | `65536` | >= 1024 | `JOURNEYPILOT_STREAMING__MAX_PENDING_TEXT_CHARS` |
| `streaming.heartbeat_seconds` | float | `15.0` | > 0 | `JOURNEYPILOT_STREAMING__HEARTBEAT_SECONDS` |
| `streaming.stalled_consumer_seconds` | float | `30.0` | > 0 | `JOURNEYPILOT_STREAMING__STALLED_CONSUMER_SECONDS` |
| `blocking_work.pdf_export` | int | `1` | >= 1 | `JOURNEYPILOT_BLOCKING_WORK__PDF_EXPORT` |
| `blocking_work.document_parse` | int | `2` | >= 1 | `JOURNEYPILOT_BLOCKING_WORK__DOCUMENT_PARSE` |
| `blocking_work.local_embedding` | int | `2` | >= 1 | `JOURNEYPILOT_BLOCKING_WORK__LOCAL_EMBEDDING` |
| `blocking_work.queue_wait_seconds` | float | `30.0` | > 0 | `JOURNEYPILOT_BLOCKING_WORK__QUEUE_WAIT_SECONDS` |
| `ingest.max_upload_bytes` | int | `10485760` | >= 1024 | `JOURNEYPILOT_INGEST__MAX_UPLOAD_BYTES` |
| `ingest.max_pdf_pages` | int | `500` | >= 1 | `JOURNEYPILOT_INGEST__MAX_PDF_PAGES` |
| `ingest.max_docx_entries` | int | `5000` | >= 1 | `JOURNEYPILOT_INGEST__MAX_DOCX_ENTRIES` |
| `ingest.max_uncompressed_bytes` | int | `104857600` | >= 1024 | `JOURNEYPILOT_INGEST__MAX_UNCOMPRESSED_BYTES` |
| `ingest.max_compression_ratio` | float | `100.0` | > 1 | `JOURNEYPILOT_INGEST__MAX_COMPRESSION_RATIO` |
| `ingest.max_extracted_chars` | int | `2000000` | >= 1024 | `JOURNEYPILOT_INGEST__MAX_EXTRACTED_CHARS` |
| `ingest.parse_timeout_seconds` | float | `60.0` | > 0 | `JOURNEYPILOT_INGEST__PARSE_TIMEOUT_SECONDS` |
| `ingest.parse_cpu_seconds` | int | `60` | >= 0 | `JOURNEYPILOT_INGEST__PARSE_CPU_SECONDS` |
| `ingest.parse_address_space_bytes` | int | `2147483648` | >= 0 | `JOURNEYPILOT_INGEST__PARSE_ADDRESS_SPACE_BYTES` |
| `provider_channels.primary_research_llm` | int | `6` | >= 1 | `JOURNEYPILOT_PROVIDER_CHANNELS__PRIMARY_RESEARCH_LLM` |
| `provider_channels.online_fast_llm` | int | `8` | >= 1 | `JOURNEYPILOT_PROVIDER_CHANNELS__ONLINE_FAST_LLM` |
| `provider_channels.ingest_contextual_llm` | int | `4` | >= 1 | `JOURNEYPILOT_PROVIDER_CHANNELS__INGEST_CONTEXTUAL_LLM` |
| `provider_channels.embedding` | int | `4` | >= 1 | `JOURNEYPILOT_PROVIDER_CHANNELS__EMBEDDING` |
| `provider_channels.max_queue_wait_seconds` | float | `180.0` | > 0 | `JOURNEYPILOT_PROVIDER_CHANNELS__MAX_QUEUE_WAIT_SECONDS` |
| `geocoding.nominatim_base_url` | str | `https://nominatim.openstreetmap.org` | — | `JOURNEYPILOT_GEOCODING__NOMINATIM_BASE_URL` |
| `geocoding.user_agent` | str | `JourneyPilot/1.0 (travel itinerary geocoder; https://github.com/journeypilot)` | — | `JOURNEYPILOT_GEOCODING__USER_AGENT` |
| `geocoding.timeout_seconds` | float | `10.0` | > 0 | `JOURNEYPILOT_GEOCODING__TIMEOUT_SECONDS` |
| `geocoding.search_timeout_seconds` | float | `3.4` | > 0 | `JOURNEYPILOT_GEOCODING__SEARCH_TIMEOUT_SECONDS` |
| `geocoding.min_interval_seconds` | float | `1.1` | — | `JOURNEYPILOT_GEOCODING__MIN_INTERVAL_SECONDS` |
| `geocoding.search_cache_ttl_seconds` | int | `604800` | > 0 | `JOURNEYPILOT_GEOCODING__SEARCH_CACHE_TTL_SECONDS` |
| `geocoding.search_cache_key_prefix` | str | `journeypilot:nominatim-search:v1` | 长度 >= 1 | `JOURNEYPILOT_GEOCODING__SEARCH_CACHE_KEY_PREFIX` |
| `geocoding.search_cache_redis_timeout_seconds` | float | `0.25` | > 0 | `JOURNEYPILOT_GEOCODING__SEARCH_CACHE_REDIS_TIMEOUT_SECONDS` |
| `routing.transitous_base_url` | str | `https://api.transitous.org` | — | `JOURNEYPILOT_ROUTING__TRANSITOUS_BASE_URL` |
| `routing.amap_base_url` | str | `https://restapi.amap.com` | — | `JOURNEYPILOT_ROUTING__AMAP_BASE_URL` |
| `routing.user_agent` | str | `JourneyPilot/2.0 (https://github.com/journeypilot)` | — | `JOURNEYPILOT_ROUTING__USER_AGENT` |
| `routing.timeout_seconds` | float | `20.0` | — | `JOURNEYPILOT_ROUTING__TIMEOUT_SECONDS` |
| `routing.min_interval_seconds` | float | `1.0` | — | `JOURNEYPILOT_ROUTING__MIN_INTERVAL_SECONDS` |
| `provider_snapshot_cache.enabled` | bool | `True` | — | `JOURNEYPILOT_PROVIDER_SNAPSHOT_CACHE__ENABLED` |
| `provider_snapshot_cache.redis_key_prefix` | str | `journeypilot:provider-snapshot:v1` | 长度 >= 1 | `JOURNEYPILOT_PROVIDER_SNAPSHOT_CACHE__REDIS_KEY_PREFIX` |
| `provider_snapshot_cache.redis_timeout_seconds` | float | `0.25` | > 0 | `JOURNEYPILOT_PROVIDER_SNAPSHOT_CACHE__REDIS_TIMEOUT_SECONDS` |
| `provider_snapshot_cache.place_identity_ttl_seconds` | int | `604800` | > 0 | `JOURNEYPILOT_PROVIDER_SNAPSHOT_CACHE__PLACE_IDENTITY_TTL_SECONDS` |
| `provider_snapshot_cache.route_ttl_seconds` | int | `300` | > 0 | `JOURNEYPILOT_PROVIDER_SNAPSHOT_CACHE__ROUTE_TTL_SECONDS` |
| `tool_exposure.mode` | str | `deferred` | — | `JOURNEYPILOT_TOOL_EXPOSURE__MODE` |
| `tool_exposure.worker_only` | bool | `True` | — | `JOURNEYPILOT_TOOL_EXPOSURE__WORKER_ONLY` |
| `tool_exposure.min_tools_threshold` | int | `8` | — | `JOURNEYPILOT_TOOL_EXPOSURE__MIN_TOOLS_THRESHOLD` |
| `model_pricing` | list[ModelPricingItem] | （按段默认） | — | — |
| `mcp_servers` | dict[str, MCPServerItem] | （按段默认） | — | — |
| `logging.level` | str | `info` | — | `JOURNEYPILOT_LOGGING__LEVEL` |
| `logging.file.enabled` | bool | `True` | — | `JOURNEYPILOT_LOGGING__FILE__ENABLED` |
| `logging.file.path` | str | `logs/` | — | `JOURNEYPILOT_LOGGING__FILE__PATH` |
| `logging.file.rotation` | str | `1 day` | — | `JOURNEYPILOT_LOGGING__FILE__ROTATION` |
| `logging.file.retention` | str | `7 days` | — | `JOURNEYPILOT_LOGGING__FILE__RETENTION` |

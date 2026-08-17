# 不变量

一条不变量说的是**什么必须永远成立、谁负责保证、哪个测试钉住它**。

它存在的理由是让代码注释可以引用一个编号，而不是在三个文件里各写一遍几百字历史。
一条不变量必须有一个 owner（保证它的那段代码）和至少一个测试 —— 没有测试的不变量
只是一句愿望，而愿望不会在有人改坏它的时候响。

**任何修复过的事故都必须永久变成测试。** 后续「整理测试」时不允许删掉它们。

设计理由不写在这里，写在 [`adr/`](adr/)。

---

## 身份与持久化

### INV-ID-001：身份是常量，客户端不声明所有者
Owner: `local_profile.py` 的 `LOCAL_USER_ID`、迁移 `migrations/versions/0003_local_identity.py`
Storage: 所有带 `user_id` 的表（取值被迁移固定）
Enforced by: 迁移把历史行归一到 `local`；接口从服务端解析归属，不读请求体里的 user
ADR: [ADR-0001](adr/ADR-0001-local-single-profile.md)
Tests:
- `test_local_identity_migration.py::*`
- `test_local_identity_api.py::*`

### INV-DB-001：API 进程不执行 DDL
Owner: `db/migrate.py`（唯一 DDL 执行者）、`infrastructure/database.py`
Storage: `alembic_version` + 结构指纹存档 `migrations/fingerprints/*.json`
Enforced by: 启动只做只读合同校验；容器 entrypoint 迁移失败即不启动 API
ADR: [ADR-0002](adr/ADR-0002-versioned-migrations.md)
Tests:
- `db/test_api_has_no_ddl.py::test_database_module_issues_no_ddl`
- `db/test_api_has_no_ddl.py::test_api_startup_and_writes_work_without_ddl_rights`
- `db/test_schema_report.py::*`

### INV-DB-002：结构指纹与 revision 必须同时匹配
Owner: `db/fingerprint.py`、`db/report.py`
Storage: `migrations/fingerprints/<revision>.json`
Enforced by: readiness 的 `database_schema` 一项拦门禁
Tests:
- `db/test_fingerprint.py::*`
- `db/test_migration_gates.py::*`

### INV-BACKUP-001：备份必须能被校验出损坏
Owner: `db/backup.py`
Storage: 备份目录里的 `manifest.json` + 每个文件的 SHA-256
Enforced by: `verify_backup` 比对大小与摘要，不只看 `pg_dump` 的退出码
Tests:
- `db/test_backup.py::test_verify_catches_a_truncated_dump`
- `db/test_backup.py::test_verify_catches_a_tampered_dump_of_the_same_size`
- `db/test_restore.py::test_corrupted_backup_is_refused_and_current_database_untouched`

### INV-BACKUP-002：备份里的密钥只留「有没有」
Owner: `config/redaction.py`（备份与 `config show` 共用同一份规则）
Enforced by: 判据是字段名而不是值的样子；输出只有 `<set>` / `<unset>`
ADR: [ADR-0008](adr/ADR-0008-config-single-source.md)
Tests:
- `db/test_backup.py::test_redaction_keeps_structure_and_drops_secrets`
- `test_config_contract.py::test_secrets_never_survive_the_effective_report`

---

## 长任务与控制

### INV-RUN-001：每个 running Run 都有有效租约
Owner: `services/run_lease.py`、`infrastructure/run_execution_store.py`
Storage: `run_execution`（executor / lease / heartbeat）
Enforced by: 心跳周期显著小于租约；连续失败达阈值后停止发起新的外部调用
ADR: [ADR-0005](adr/ADR-0005-restart-does-not-resume-spending.md)
Tests:
- `db/test_run_execution_lease.py::*`
- `test_run_execution_contract.py::*`

### INV-RUN-002：孤儿 Run 不会永久显示 running
Owner: `services/run_recovery.py`
Storage: `trip_runs.status` + `run_execution`
Enforced by: 启动先扫一次，之后周期复扫（上一个进程死时租约可能还剩几十秒）
ADR: [ADR-0005](adr/ADR-0005-restart-does-not-resume-spending.md)
Tests:
- `db/test_run_recovery.py::*`

### INV-RUN-003：重启后不自动继续花费
Owner: `services/run_recovery.py`、`workflows/run_budget.seed_run_budget`
Storage: `trip_runs.status` = interrupted / resume_available；`run_llm_calls`（已花量）
Enforced by: 恢复只改状态不发调用；恢复的 Run 从台账读回已花量而不是拿满额预算
ADR: [ADR-0005](adr/ADR-0005-restart-does-not-resume-spending.md)
Tests:
- `test_run_budget.py::test_a_resumed_run_reads_its_spend_back_from_the_ledger`
- `test_run_budget.py::test_an_unreadable_ledger_starts_at_zero_instead_of_blocking`

### INV-CMD-001：cancel/supplement 不依赖发请求命中的那个进程
Owner: `services/run_commands.py`、`workflows/run_control.RunControlRegistry`
Storage: `trip_run_commands`（最终事实）；registry 只是唤醒通道
Enforced by: 先落库再通知；执行器在协作边界轮询并幂等消费
ADR: [ADR-0004](adr/ADR-0004-durable-local-jobs.md)
Tests:
- `db/test_run_control_api.py::test_a_real_executor_picks_the_command_up_from_the_table`
- `db/test_run_control_api.py::test_cancel_is_accepted_with_a_live_executor_elsewhere`
- `test_run_command_contract.py::*`

### INV-CMD-002：点几次取消都是一条命令
Owner: `services/run_commands.py`（按指纹去重）
Storage: `trip_run_commands` 的唯一约束
Tests:
- `db/test_run_commands.py::test_cancel_is_one_command_however_many_times_it_is_clicked`
- `db/test_run_control_api.py::test_clicking_stop_again_returns_the_same_receipt`

### INV-CMD-003：终态不因取消而说谎
Owner: `services/run_commands.py`、`workflows/run_control.RunStopReason`
Enforced by: 用户取消→CANCELLED；失去租约/响应流退出→INTERRUPTED，两者不混
Tests:
- `test_run_command_contract.py::test_a_completed_run_does_not_pretend_the_cancel_was_carried_out`
- `db/test_run_recovery.py::test_cancel_requested_converges_to_cancelled`

### INV-JOB-001：后台任务可重试、去重、恢复
Owner: `services/background_jobs.py`、`infrastructure/background_job_store.py`
Storage: `background_jobs`（lease / attempt / backoff / dedupe key）
Enforced by: claim 一次只给一个消费者；租约过期后重新可领；耗尽 attempt 进 dead
ADR: [ADR-0004](adr/ADR-0004-durable-local-jobs.md)
Tests:
- `db/test_background_jobs.py::test_claim_takes_each_job_once`
- `db/test_background_jobs.py::test_a_claimed_job_is_not_claimable_until_its_lease_expires`
- `db/test_background_jobs.py::test_attempts_run_out_into_dead`
- `test_memory_extraction_job.py::*`

---

## 流与会话

### INV-SSE-001：每个请求恰好一个终态帧
Owner: `api/routes/chat.py` 的 `cleanup_stream_exit`（唯一终态发布点）
Storage: `chat_requests`（单向终态转移）
Enforced by: 所有退出路径收敛到一处；completed 之后不许再发 error
ADR: [ADR-0003](adr/ADR-0003-sse-terminal-model.md)
Tests:
- `test_sse_buffer.py::test_a_critical_event_never_jumps_ahead_of_earlier_text`
- `test_sse_buffer.py::test_a_full_critical_queue_makes_the_producer_wait`

### INV-SSE-002：慢客户端不让内存无界增长，且不丢一个字符
Owner: `api/sse_buffer.py`
Storage: 进程内，上限在 `Settings.streaming`
Enforced by: critical 队列有界且不丢；token 合并成更大 chunk 而不是丢弃；
待发正文超上限即强制 flush；消费者卡住则结束传输交给 durable 恢复
ADR: [ADR-0006](adr/ADR-0006-resource-budgets-and-isolation.md)
Tests:
- `test_sse_buffer.py::test_a_slow_client_keeps_every_character_and_every_critical_event`
- `test_sse_buffer.py::test_adjacent_tokens_merge_without_losing_characters`
- `test_sse_buffer.py::test_a_consumer_that_never_reads_is_reported_stalled`

### INV-SESSION-001：一个 turn 永远整块返回
Owner: `api/routes/sessions.py` 的 turn 分页、`chat_session_events.turn_id`
Storage: `chat_session_events(session_id, turn_id, event_order)`
Enforced by: cursor 用稳定的 first event order，不用 offset
Tests:
- `db/test_session_turns.py::test_a_turn_is_always_returned_whole`
- `db/test_session_turns.py::test_a_new_turn_does_not_shift_an_open_cursor`
- `db/test_session_turns.py::test_a_page_stops_at_the_event_budget`

### INV-SESSION-002：压缩只读预算范围，boundary 精确
Owner: `memory/compaction.py`（`commit_compaction` 是唯一写入方）
Storage: session anchor + boundary + compaction event（同事务）
Enforced by: boundary 只推进到**实际进入摘要的最后一条**；失败不推进也不覆盖旧 anchor
Tests:
- `db/test_session_turns.py::test_compaction_only_reads_the_budget_and_moves_the_exact_boundary`
- `db/test_session_turns.py::test_a_second_compaction_is_incremental`
- `db/test_session_turns.py::test_a_failed_compaction_leaves_the_anchor_and_boundary_alone`

---

## 资源边界

### INV-BLOCK-001：同步工作不阻塞 Event Loop，且并发有上限
Owner: `services/blocking_work.py`
Storage: 进程内通道，上限在 `Settings.blocking_work`
Enforced by: PDF 渲染、文档解析、本地 embedding 一律走受限线程/子进程；
排队有上界，等不到位置抛 `BlockingWorkBusy` 而不是无限排队
ADR: [ADR-0006](adr/ADR-0006-resource-budgets-and-isolation.md)
Tests:
- `test_blocking_work.py::test_blocking_call_does_not_stall_the_event_loop`
- `test_blocking_work.py::test_channel_limit_caps_concurrent_thread_work`
- `test_blocking_work.py::test_queue_wait_has_an_upper_bound`

### INV-INGEST-001：不可信文档先过输入边界，再进受限解析单元
Owner: `rag/sources/document_parse.py` + `rag/sources/document_parser_worker.py`
Storage: 上限在 `Settings.ingest`
Enforced by: 类型（magic bytes + OOXML 必要条目）→ 规模（字节 / 展开量 / 压缩比 /
条目数）→ 解析（子进程 + RLIMIT + 超时杀进程组）。**不靠超时当唯一保护**
ADR: [ADR-0006](adr/ADR-0006-resource-budgets-and-isolation.md)
Tests:
- `test_document_parse.py::test_zip_bomb_is_rejected_on_compression_ratio`
- `test_document_parse.py::test_zip_bomb_is_rejected_on_expanded_size`
- `test_document_parse.py::test_a_pdf_suffix_over_non_pdf_bytes_is_rejected`
- `test_document_parse.py::test_a_pdf_over_the_page_cap_is_rejected`
- `test_document_parse.py::test_parser_timeout_kills_the_subprocess`

### INV-BUDGET-001：预算在调用之前判，快照不受配置热更新影响
Owner: `entities/run_budget.py`（判据）、`workflows/run_budget.py`（账本与守卫）
Storage: `RunBudgetSnapshot` 随 checkpoint；已花量的最终事实在 `run_llm_calls`
Enforced by: 按最坏开销在调用前判；快照在 Draft 授权时封存，与 Deadline 同进同出
ADR: [ADR-0006](adr/ADR-0006-resource-budgets-and-isolation.md)
Tests:
- `test_run_budget.py::test_worst_case_estimate_blocks_the_call_that_would_overspend`
- `test_run_budget.py::test_a_sealed_snapshot_ignores_later_config_changes`
- `test_run_budget_boundaries.py::test_budget_and_deadline_must_be_sealed_together`
- `test_run_budget_boundaries.py::test_a_replay_of_a_sealed_draft_never_refills_the_budget`

### INV-BUDGET-002：预算耗尽是可解释的降级，不是一次 Run 失败
Owner: `agents/utils.execute_tool`、`workflows/run_budget.RunBudgetExhausted`
Enforced by: 工具侧返回带 `run_budget_exhausted.<维度>` 的 failed envelope，
交给 Candidate Gate；不抛异常、不记成 CANCELLED
Tests:
- `test_run_budget_boundaries.py::test_tool_call_over_budget_returns_a_failed_envelope_not_an_exception`
- `test_run_budget_boundaries.py::test_a_tool_that_used_up_its_retries_is_not_called_again`

### INV-BUDGET-003：低报的费用不许当上限用
Owner: `entities/run_budget.exhausted_dimension`
Enforced by: 价格表未命中时 `cost_complete=false`，费用维不参与判定；
低报本身随成本摘要报出去
Tests:
- `test_run_budget.py::test_an_unpriced_call_never_lets_cost_reject_a_call`

### INV-CHAN-001：入库不许把在线请求排到队尾
Owner: `utils/concurrency.py`、`models/router.llm_channel`
Storage: 进程内通道，配额在 `Settings.provider_channels`
Enforced by: contextual 分块走 `ingest_contextual_llm`，与 `online_fast_llm` 分开计
ADR: [ADR-0006](adr/ADR-0006-resource-budgets-and-isolation.md)
Tests:
- `test_provider_channels.py::test_ingest_and_online_calls_do_not_share_a_quota`
- `test_provider_channels.py::test_changing_the_limit_replaces_the_gate`

---

## 配置与交付

### INV-CFG-001：未知配置字段与拼错的环境变量都是错误
Owner: `config/models.StrictConfig`（`extra="forbid"`）、`config/env.py`
Enforced by: 校验失败带字段路径、当前值与合法范围；认不出的 `JOURNEYPILOT_*` 阻止启动
ADR: [ADR-0008](adr/ADR-0008-config-single-source.md)
Tests:
- `test_config_contract.py::test_an_unknown_field_is_rejected_with_its_path_and_value`
- `test_config_contract.py::test_a_misspelled_env_variable_is_an_error_not_a_no_op`
- `test_config_contract.py::test_a_wrong_typed_env_value_is_an_error_not_a_fallback`

### INV-CFG-002：每个生效值都能说出它从哪来
Owner: `config/loader.EffectiveConfig`
Enforced by: 来源三档 config default / config.yaml / environment (VAR)
ADR: [ADR-0008](adr/ADR-0008-config-single-source.md)
Tests:
- `test_config_contract.py::test_effective_config_states_where_each_value_came_from`

### INV-CFG-003：配置文档与 schema 不许漂移
Owner: `config/schema_export.py`、`journeypilot config docs --check`
Storage: `docs/configuration.md`、`docs/config.schema.json`（生成物，提交进仓库）
Enforced by: 测试与 CI 都比对生成物与当前 schema
ADR: [ADR-0008](adr/ADR-0008-config-single-source.md)
Tests:
- `test_config_contract.py::test_generated_config_docs_are_committed`
- `test_config_contract.py::test_the_example_config_is_valid_and_current`

### INV-CFG-004：示例配置不带 API Key
Owner: `config.example.yaml`、`config/providers.preset_model_section`
Enforced by: preset 只写连接段；Key 走环境变量或 `.env`
ADR: [ADR-0008](adr/ADR-0008-config-single-source.md)
Tests:
- `test_config_contract.py::test_the_example_config_ships_no_api_key`
- `test_config_contract.py::test_a_preset_section_never_contains_an_api_key`

### INV-PROV-001：Provider 兼容性来自声明，不靠猜 base_url
Owner: `config/providers.py`、`configs/providers/*.yaml`
Enforced by: capability 按 host（带端口）解析；认不出的上游走保守档
ADR: [ADR-0008](adr/ADR-0008-config-single-source.md)
Tests:
- `test_config_contract.py::test_direct_and_proxied_deepseek_get_different_capabilities`
- `test_config_contract.py::test_an_unknown_endpoint_gets_the_conservative_profile`
- `test_config_contract.py::test_a_local_service_on_another_port_is_not_ollama`
- `test_config_contract.py::test_reasoning_dialects_follow_the_declaration`

### INV-DEP-001：配置要求的增强能力没装就不启动
Owner: `capabilities.py`
Enforced by: 启动前探测（不 import 模型），缺失即拒绝并给出安装命令；
readiness 的 `optional_capabilities` 拦门禁
ADR: [ADR-0007](adr/ADR-0007-core-default-enhancements-optional.md)
Tests:
- `test_capabilities.py::*`
- CI：`core-install` 作业验证 core 安装不需要 cross encoder

### INV-UI-001：后端能发的每一个失败 code，界面都有自己的一句话
Owner: `api/routes/knowledge.py` 与 `frontend/src/lib/knowledgeIngestFailure.ts`
Enforced by: 前端测试**读后端源文件**，要求两张表差集为空
Tests:
- `frontend/src/lib/knowledgeIngestFailure.test.ts`（双向差集 + 每个 code 有自己的话）

# 发布检查单

版本号分层，**不用一个号解释所有层**：应用 SemVer、DB migration revision、
`config_version`、SSE 协议版本、Delivery contract 版本各自独立。

## 自动跑的部分

打 `v*` tag 触发 [`.github/workflows/release.yml`](../.github/workflows/release.yml)：
冻结依赖、空库安装到 head、全量测试、配置生成物一致性、备份恢复矩阵、
amd64/arm64 镜像、非 root 与 PDF 字体验证、容器扫描、SBOM。

真实 Provider 冒烟是**手动勾选**的可选作业（`workflow_dispatch` 的 `live_smoke`），
用专用低额度 Key（[ADR-0009](adr/ADR-0009-ci-never-spends-on-providers.md)）。

## Release notes 必须回答的问题

读的人要决定「我能不能升，升之前要做什么」。所以每一条都必须有答案，
**没有变化也要写「无」** —— 空着与「无」不是同一个意思。

- [ ] 有没有 DB migration？是哪几个 revision？
- [ ] 升级会不会自动备份？（默认会；`--skip-backup` 才不会）
- [ ] 有没有破坏性迁移（丢数据或不可逆）？需要 `--allow-destructive` 吗？
- [ ] 配置字段有什么变化？`config_version` 变了吗？旧文件会被自动迁移还是要手改？
- [ ] 默认监听地址/端口有变化吗？
- [ ] 依赖或平台要求有变化吗？依赖分组变了吗？
- [ ] 需要重建索引吗？（切换 zhparser profile、改 embedding 维度都需要）
- [ ] 可回滚到哪个版本？回滚需要恢复备份吗？
- [ ] 已知限制是什么？

## 交付产物

- [ ] source tag
- [ ] container image digest（多架构 index）
- [ ] checksums
- [ ] SBOM
- [ ] 升级指南（如果这一版需要人工步骤）
- [ ] `config.example.yaml`
- [ ] 一份 `journeypilot doctor --json` 的示例输出

## 规划合同

- [ ] clause ledger 覆盖每条实质性输入，没有静默丢弃的 hard intent
- [ ] `CapabilityPlan` 为每个活跃 hard intent 指定所有者与 success criteria
- [ ] plan gate 与运行中 supplement 都通过 `IntentAmendment` 回到 Request Contract
- [ ] Packet、Catalog、Workspace、Manifest 的 `generation_id` 一致，旧代 Packet 不能进 Catalog
- [ ] 后端与前端 Delivery contract 版本同步
- [ ] `ResearchQueryPlan` 与 `CapabilityPlan.research_query_ids` 属于同一 generation 和 Intent revision
- [ ] Generic Fallback 只在 Intent Primary、Structural 均执行且候选不足时调用
- [ ] 显式排除类别没有进入 Provider Query，排除后的候选缺口没有被通用候选强行补齐
- [ ] Candidate Discovery Lineage 来自服务端执行记录，不接受模型自报 Intent/Query ID
- [ ] Admission、Intent Evaluation、Ranking 与 Selection 分层，软主题变化不改写 Admission
- [ ] Composer 只读取 `CandidateSelectionPlan` 允许的候选

## 检索质量变化

默认数据库不再构建 zhparser（[ADR-0007](adr/ADR-0007-core-default-enhancements-optional.md)）。
中文分词质量的变化必须**明确写进 release notes**，不能用「能运行」冒充「质量相同」。
建立固定中文检索基准后，把 simple 与 zhparser 的对比数字附在这里。

# ADR-0008：配置只有一处定义，来源可查

状态：已采纳
影响：`config/` 包、`configs/providers/*.yaml`、`docs/configuration.md`、
`journeypilot config *`、`run.sh`、`docker-compose.yml`

## 决定

- 字段、默认值与校验只在 `config/models.py`；未知字段**拒绝**（`extra="forbid"`）。
- 环境变量统一 `JOURNEYPILOT_<段>__<字段>`，一处按 schema 解析；认不出的
  `JOURNEYPILOT_*` 变量与类型不符的值都是**错误**。
- 启动脚本、Compose、README **不定义任何策略**（Deadline、预算、超时、端口默认值）。
- Provider 的具体连接值是 **preset**（`configs/providers/*.yaml`），不是默认值。
- 字段参考表与 JSON Schema 由 schema 生成，CI 检查 diff 为空。

## 为什么

同一条策略有三个 owner 时，改了一处之后另外两处继续生效，而**没有一处读数能解释
为什么**。`run.sh` 里 export 一份 Deadline、Compose 里另一份、代码里第三份，正是
这个形态。

「未知字段静默忽略」是「我改了 YAML 为什么没生效」的唯一来源。同理，一个拼错的环境
变量名如果什么都不做，那么它和没设是无法区分的。

把某家 provider 的 base_url 写成 Pydantic 默认值，等于让一次私人部署成为所有人的
默认，而那个人换了 provider 之后没有一处会跟着改。

## 替代方案与为什么没选

- **保留旧环境变量名做兼容期**：产品没有用户，兼容层的唯一作用是让两套名字同时存在。
- **手写字段文档**：它在第一次改默认值时就落后了，而落后的文档比没有文档更贵。

## 后果

- `journeypilot config show` 报出每个值的来源（config default / config.yaml /
  environment (VAR)），直接回答「我改了 YAML 为什么没生效」。
- API Key 从不写进 `config.yaml`；`configure --provider` 只写连接段。
- 密钥在任何输出里只有 `<set>` / `<unset>`，不给前缀加星号 —— 那会把密钥的一部分
  交出去，而这份输出正是用户会贴出来的东西。

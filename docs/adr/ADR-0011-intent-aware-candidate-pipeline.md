# ADR-0011：意图驱动研究与候选选择分层

## 决定

在 `IntentSpec` 与 Research Worker 之间建立服务端生成的 `ResearchQueryPlan`。Worker
只执行 assignment 绑定的 Query ID，并按 Intent Primary、Structural、Generic Fallback、
Targeted Repair 的层级研究。Generic Fallback 由独立政策控制，显式排除的类别在 Provider
调用前被禁止。

候选处理拆成四个边界：

1. Admission 只判断事实、来源与硬 Constraint 是否允许候选进入 Catalog；
2. Candidate Intent Evaluation 只基于已验证事实判断候选与 Intent 的关系；
3. Ranking 使用不可补偿的分层排序，硬违规不能被软偏好分数抵消；
4. Selection 从已准入候选中确定 Primary、Alternative 与 Fallback，Composer 只读取
   `CandidateSelectionPlan` 允许的候选。

正式 Query、Intent 与 Candidate Discovery Lineage 由 Packet Compiler 根据服务端执行记录
生成，Worker 模型不能声明。Candidate Gate 根据未覆盖 Intent 创建 Targeted Repair Query，
不再发送无范围的“补充更多候选”。

## 理由

通用 museum、restaurant、hotel 查询直接作为研究入口时，不同用户意图会得到几乎相同的
候选池。把“事实为真”“符合偏好”“本次要选”混在 Admission 或 Composer 中，又会让通过
准入的每个候选都产生排入行程的压力。分层合同使查询来源、事实准入、意图评估、排序和
选择可以分别验证，并让软主题变化只影响评估与选择，不改写事实准入结果。

## 替代方案与未采用理由

- 继续让 Worker 根据自然语言任务自行查询：无法证明 Generic Fallback 的执行条件，也无法
  建立稳定 Query Lineage。
- 把偏好直接做成 Admission 总分：软偏好会错误淘汰真实候选，硬违规也可能被其他分数抵消。
- 把所有 admitted candidates 交给 Composer：Catalog 的事实生命周期与本次选择决策混在一起，
  通过准入会变成隐含 Placement 义务。
- 由模型输出正式 Intent/Query ID：执行来源可被模型自报，无法作为审计事实。

## 结果

Research Packet 升级为 v5，Recommendation Catalog 升级为 v6；包含它们的 Delivery Bundle
与 Trip Workspace 升级为 v9。旧本地 payload 和 fixture 直接替换，不提供兼容读取层。

## 对应不变量

INV-RESEARCH-001、INV-RESEARCH-002、INV-CANDIDATE-001、
INV-CANDIDATE-002、INV-CANDIDATE-003、INV-CANDIDATE-004。

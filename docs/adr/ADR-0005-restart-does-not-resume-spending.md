# ADR-0005：重启后不自动继续花费额度

状态：已采纳
影响：`services/run_recovery.py`、`services/run_lease.py`、`workflows/run_budget.py`

## 决定

进程重启后发现租约过期的 `running` Run：校验 checkpoint，然后转为
`interrupted` / `resume_available`，UI 显示「上次运行被程序关闭中断」。
**只有用户显式点继续**才重新调用模型。

一个被恢复的 Run 还要把已花掉的量从 `run_llm_calls` 台账读回本进程的预算账本，
所以它拿到的不是一份满额预算。

## 为什么

这是个人自托管软件：用户重启电脑不应该在后台未经确认地继续产生 API 费用。
自动恢复在服务端产品里是对的默认，在这里不是。

## 替代方案与为什么没选

- **自动续跑**：省一次点击，代价是一个不知情的账单。
- **直接判失败**：checkpoint 还在，判失败等于丢掉已经花掉的那些钱买到的东西。

## 后果

- 每个 running Run 都必须有有效 lease/heartbeat，否则无法区分「活着」与「孤儿」。
- 恢复扫描要周期复扫，不能只在启动时扫一次（上一个进程死时租约可能还剩几十秒）。

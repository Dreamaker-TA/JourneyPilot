"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

## 每条迁移必须回答的六个问题（dev docs P0 §6 强制门禁）

- 影响面：后端 / 前端 / DB / Compose / 配置 / 文档分别是否变化？
- 旧数据路径：既有行怎么读、怎么迁、能不能回滚？
- 失败注入：事务中间失败会留下什么？
- 幂等：跑两次会不会重复写入？
- 观察信号：失败后 doctor / readiness / 日志显示什么？
- 回滚：能 downgrade 吗？不能就只能从备份恢复 —— 在这里写清楚。

## 破坏性标记

删列、删表、删行、不可逆改写的迁移，把下面这行改成 True。
`journeypilot migrate` 不会自动执行它，必须显式 `--allow-destructive`。
"""

from __future__ import annotations

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

#: 会丢数据或不可逆的迁移 → True，`migrate` 需要 --allow-destructive 才执行。
destructive = False

#: 能不能 downgrade。False 时唯一的回退路径是从备份恢复，release notes 必须写明。
reversible = True


def upgrade() -> None:
    ${upgrades if upgrades else "raise NotImplementedError"}


def downgrade() -> None:
    ${downgrades if downgrades else "raise NotImplementedError"}

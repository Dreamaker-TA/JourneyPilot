import React from 'react';
import { cn } from '../../lib/utils';
import { READOUT_LABEL } from '../../lib/typography';
import { RollingNumber } from '../ui/RollingNumber';
import { InspectRow, InspectSectionTitle } from '../ui/InspectHint';
import type {
  CostLedgerBreakdown,
  CostLedgerNotice,
  CostLedgerSaving,
  CostLedgerTile,
  CostLedgerView,
} from '../../lib/costLedger';

/**
 * 成本台账浮窗（`cost ¤`）。
 *
 * 这一屏是删掉 `CostBlock.tsx` 之后台账的**唯一**呈现面。形态：一枚静默印记后面的
 * 浮窗 —— 默认那一屏上什么都没有，好奇的人点一下才看到（分层披露）。
 *
 * **数从哪来**：`lib/costLedger.ts` 的投影。这里一次算术都没有，连比率都不算 ——
 * 每一个数都带着它在台账里的字段名（`data-ledger-field`），按那张名单逐条量
 * 它在不在屏幕上。
 *
 * **排版**（Typography）：11px 是地板；`tabular-nums` /
 * `font-mono` 只落在纯数字节点上，中文标签与数值各自成节点 —— CJK 字形在等宽数字
 * 特性下会把全角标点撑成一个数字格。
 */

const TILE_VALUE = 'text-[13px] font-semibold tabular-nums';

/** 读数瓦片。数值走 `RollingNumber` 逐位滚动（§5.2），标签是另一个节点。 */
const Tile: React.FC<{ tile: CostLedgerTile }> = ({ tile }) => (
  <div
    data-ledger-field={tile.field}
    data-ledger-display={tile.estimated ? `≈${tile.display}` : tile.display}
    className="flex min-w-0 flex-col items-center gap-1 rounded-card bg-surface px-1.5 py-2"
  >
    {tile.value == null ? (
      <span className={cn(TILE_VALUE, tile.accent ? 'text-accent' : 'text-ink')}>{tile.display}</span>
    ) : (
      <RollingNumber
        value={tile.value}
        format={() => tile.display}
        prefix={tile.estimated ? '≈' : undefined}
        className={cn(TILE_VALUE, tile.accent ? 'text-accent' : 'text-ink')}
      />
    )}
    <span className="max-w-full truncate text-[11px] text-ink-muted">{tile.label}</span>
  </div>
);

/**
 * 按环节分解。条宽是几何（`barShare`），台账里的数原样印在文字里 ——
 * 此前那版把 `cost_usd / total_cost_usd` 当百分数印出来，那是个没人负责的数。
 */
const Breakdown: React.FC<{ breakdown: CostLedgerBreakdown }> = ({ breakdown }) => (
  <section data-ledger-field={breakdown.field} className="flex flex-col gap-2">
    <InspectSectionTitle>按环节</InspectSectionTitle>
    <ul className="flex flex-col gap-2">
      {breakdown.rows.map((row) => (
        <li key={row.key} className="flex flex-col gap-1">
          <div className="flex items-baseline justify-between gap-2">
            <span className="min-w-0 truncate text-[11px] text-ink">{row.label}</span>
            <span className="shrink-0 font-mono text-[11px] tabular-nums text-ink-secondary">
              {row.tokens} · {row.cost}
            </span>
          </div>
          <div className="h-1 overflow-hidden rounded-full bg-highlight">
            <div
              className="h-full rounded-full bg-accent"
              style={{ width: `${Math.max(2, Math.round(row.barShare * 100))}%` }}
            />
          </div>
          <span className="font-mono text-[11px] tabular-nums text-ink-muted">
            {row.calls}× · {row.latency}
          </span>
        </li>
      ))}
    </ul>
    {breakdown.rest > 0 && (
      <span className="text-[11px] text-ink-muted">另有 {breakdown.rest} 个环节未列出</span>
    )}
  </section>
);

/** Tool Search 上下文节省量（实测；REST 路径拿不到，为空则整段不渲染）。 */
const Saving: React.FC<{ saving: CostLedgerSaving }> = ({ saving }) => (
  <section
    data-ledger-field={saving.field}
    className="flex flex-col gap-1 rounded-card bg-surface px-3 py-2"
  >
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[11px] text-ink">工具说明用量节省</span>
      <span className="shrink-0 font-mono text-[11px] font-semibold tabular-nums text-success">
        {saving.ratio}
      </span>
    </div>
    <span className="font-mono text-[11px] tabular-nums text-ink-muted">
      {saving.savedTokens} · {saving.exposure}
    </span>
    <span className="text-[11px] text-ink-muted">{saving.modeLabel}</span>
  </section>
);

const Notice: React.FC<{ notice: CostLedgerNotice }> = ({ notice }) => (
  <p
    data-ledger-field={notice.field}
    className={cn(
      'rounded-card px-3 py-2 text-xs leading-relaxed',
      notice.tone === 'warning'
        ? 'bg-[color-mix(in_srgb,var(--color-warning)_8%,var(--color-surface))] text-ink-secondary'
        : 'bg-surface text-ink-secondary'
    )}
  >
    {notice.text}
  </p>
);

export const CostLedger: React.FC<{ view: CostLedgerView }> = ({ view }) => (
  <div
    data-testid="cost-ledger"
    data-ledger-source={view.source}
    className="flex max-h-[min(70vh,520px)] flex-col gap-3 overflow-y-auto p-4"
  >
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[13px] font-semibold text-ink">本轮模型用量</span>
      {view.source === 'live' && (
        <span className={cn(READOUT_LABEL, 'shrink-0 text-ink-muted')}>LIVE</span>
      )}
    </div>

    <div
      className={cn('grid gap-1.5', view.tiles.length >= 4 ? 'grid-cols-4' : 'grid-cols-3')}
    >
      {view.tiles.map((tile) => (
        <Tile key={tile.field} tile={tile} />
      ))}
    </div>

    {view.activeLabel && (
      <p className="text-[11px] leading-relaxed text-ink-secondary">
        正在累计 · {view.activeLabel}
      </p>
    )}

    <div className="flex flex-col gap-1">
      {view.rows.map((row) => (
        // `InspectRow` 是检查面键值行的既有声部（等宽值 + 右对齐）；这里只包一层
        // 挂字段名的容器，不复制它的排版。
        <div key={row.field} data-ledger-field={row.field}>
          <InspectRow label={row.label} value={row.display} />
        </div>
      ))}
    </div>

    {view.breakdown && <Breakdown breakdown={view.breakdown} />}
    {view.saving && <Saving saving={view.saving} />}
    {view.notices.map((notice) => (
      <Notice key={`${notice.field}:${notice.tone}`} notice={notice} />
    ))}

    {/* 教育层一句（§4.2 第 4 段同一档）：一句陈述，不吹。 */}
    <p className="text-xs leading-relaxed text-ink-secondary">
      每一次模型调用都逐条落进运行台账，这里的数字是那些行的合计。
    </p>
  </div>
);

/**
 * Shared display formatting for observability readouts (data-dense readout
 * discipline — tabular numerals, single format across surfaces).
 *
 * One source of truth so the same `wall_ms` / `latency_ms` / step duration reads
 * identically wherever it appears — the思维链 total, 成本台账的读数瓦片, per-node
 * latency, and planning-progress elapsed all draw from here. Before
 * this, ThinkingChain rounded seconds without ever rolling to minutes (`649s`)
 * while the cost block rolled to minutes (`10m49s`) for the very same value.
 *
 * **这一层只格式化，不算数。** 台账里的比率（`estimated_ratio` /
 * `cost_coverage_ratio`）由后端 `cost_ledger_store.summarize_calls` 算好，这里只把
 * 0.4267 印成 `43%`。`formatPercent` 收的是**比率**而不是分子分母，就是为了让
 * 「前端再除一次」在类型上写不出来 —— 一个数由哪一层负责，就只在那一层写一次。
 */

/** 无值时的读数占位。所有读数共用这一枚，不许某处写 `-`、某处写 `N/A`。 */
export const READOUT_PLACEHOLDER = '—';

/**
 * Duration readout from milliseconds. One rule全站沿用 (metric typography):
 * seconds with one decimal below a minute, minutes-and-seconds above.
 *
 * - `< 60s` → `x.xs` (one decimal; e.g. `800ms → 0.8s`, `12345ms → 12.3s`)
 * - `≥ 60s` → `Xm YYs` (zero-padded seconds; e.g. `649000ms → 10m49s`)
 *
 * `null` / `undefined` / non-finite → the em-dash placeholder.
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return READOUT_PLACEHOLDER;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const minutes = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  // A rounded remainder can land on 60 (e.g. 119.6s → 1m60s); carry it up.
  if (rem === 60) return `${minutes + 1}m00s`;
  return `${minutes}m${rem.toString().padStart(2, '0')}s`;
}

/**
 * 货币符号。台账下发的是 ISO 代码（`run_cost_summary.currency`，当前恒为 `USD`）；
 * 符号是**显示**的事，所以归这里，而不是让后端往 SSE 里塞一个 `$`。
 */
export function currencySymbol(currency: string | null | undefined): string {
  switch (currency) {
    case 'CNY':
    case 'RMB':
      return '¥';
    case 'EUR':
      return '€';
    default:
      return '$';
  }
}

/** 整数计数（调用数、腿数）。千分位，纯数字。 */
export function formatCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return READOUT_PLACEHOLDER;
  return Math.round(value).toLocaleString('en-US');
}

/** Token 明细：千分位全量（明细行用，读者会去核对的那一档）。 */
export function formatTokens(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return READOUT_PLACEHOLDER;
  return Math.round(value).toLocaleString('en-US');
}

/**
 * 紧凑 token（读数瓦片那一档窄格用，避免六位数被截断）。
 *
 * `< 1000` 原样；`< 1M` 走 `K`（≥100K 时不留小数，否则一位）；再往上走 `M` 两位。
 */
export function formatTokensCompact(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return READOUT_PLACEHOLDER;
  const n = Math.round(value);
  if (n < 1000) return n.toLocaleString('en-US');
  if (n < 1_000_000) {
    const k = n / 1000;
    return `${k >= 100 ? Math.round(k) : k.toFixed(1)}K`;
  }
  return `${(n / 1_000_000).toFixed(2)}M`;
}

/**
 * 金额。
 *
 * `null` → 占位符：**未命中价目表的一轮不编造 `$0.00`**（后端已经在
 * `summarize_calls` 里做了同一个决定 —— `priced_call_count == 0` 时 `total_cost_usd`
 * 报 `None`，这里只是不把那个 `None` 洗成 0）。
 * `0 < x < 0.01` → `<$0.01`：两位小数会把一次真实花费印成 `$0.00`，那比不报更糟。
 */
export function formatCost(value: number | null | undefined, currency?: string | null): string {
  if (value == null || !Number.isFinite(value)) return READOUT_PLACEHOLDER;
  const symbol = currencySymbol(currency);
  if (value === 0) return `${symbol}0.00`;
  if (value > 0 && value < 0.01) return `<${symbol}0.01`;
  return `${symbol}${value.toFixed(2)}`;
}

/**
 * 百分比。**收的是后端已经算好的比率**（0.4267 → `43%`），不收分子分母 ——
 * 见本文件头注释：比率由台账那一层负责，这里再除一次就是第二份账。
 */
export function formatPercent(ratio: number | null | undefined, digits = 0): string {
  if (ratio == null || !Number.isFinite(ratio)) return READOUT_PLACEHOLDER;
  return `${(ratio * 100).toFixed(digits)}%`;
}

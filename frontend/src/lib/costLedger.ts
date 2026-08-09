import type { RunCostLive } from '../context/AppContext';
import type { CostGroupBreakdown, RunCostSummary, ToolContextSaving } from '../types/api';
import {
  formatCost,
  formatCount,
  formatDuration,
  formatPercent,
  formatTokens,
  formatTokensCompact,
  READOUT_PLACEHOLDER,
} from './format';
import { formatAgentLabel } from './travelProgress';

/**
 * 成本台账的**上屏投影**（`cost ¤`）。
 *
 * ## 为什么需要这一层
 *
 * 后端 `cost_ledger_store.summarize_calls` 一次算出 23 个字段，SSE 逐次下发
 * `usage_update` 的 16 个键。这些数**只由那一层负责**：比率、瓶颈排序、分组聚合、
 * 「未命中价目表就报 null 而不是 0」——全部已经决定完了。这一层做的事只有两件：
 *
 * 1. **裁决**：哪个字段上屏、哪个不上屏。两张名单（`ON_SCREEN` / `OFF_SCREEN`）把
 *    每一个字段登记恰好一次，拿 `Required<RunCostSummary>` 的键集合与两张名单对账 ——
 *    后端加一个字段而没人裁决它，就会静默不上屏。
 *    这挡的是这个仓最贵的形状：**23 个数算出来、1 个上屏，而没有任何一处写着为什么。**
 * 2. **取值与格式化**：把台账里的值取出来交给 `lib/format.ts`。**这里一次算术都没有** ——
 *    没有除法、没有求和、没有排序。想印一个后端没算的数，只能先去后端加。
 *
 * ## 双账本（§5.1）
 *
 * - `settled`：`chat_complete.run_cost_summary`。run 终结后的权威值。
 * - `live`：`AppContext.runCostLive`，由 `usage_update` 逐次累积 —— 每一个 token 都对应
 *   台账里落库的一行，所以它**也是**真值，只是还没收口。
 *
 * **两个账本不混算**：`run_cost_summary` 一到就整块接管，绝不「settled 的成本 + live 的
 * 调用数」拼一份出来。§5.1 原文里那个 `字符数 / 4` 的 in-flight 估算分量**产品里不存在**
 * （`useSendMessage` 只留了注释，累加那个变量已经不在了）—— 所以这里没有 in-flight，
 * `≈` 的唯一来源是后端 `estimated` 标记，见 `estimated` 字段。
 */

// ---------------------------------------------------------------------------
// 裁决：哪些数上屏
// ---------------------------------------------------------------------------

/** 台账字段名 —— 判据用它在 DOM 里定位读数（`data-ledger-field`）。 */
export type LedgerField = keyof RunCostSummary;

/**
 * **上屏**的台账字段。每一条在浮窗里都有一个挂着 `data-ledger-field` 的节点，
 * `hallmark-cost-ledger.spec.ts` 按这张表逐条量「这个数在屏幕上找得到、且等于台账里的值」。
 */
export const ON_SCREEN_LEDGER_FIELDS = [
  'call_count',
  'total_tokens',
  'total_input_tokens',
  'total_output_tokens',
  'total_cached_input_tokens',
  'total_reasoning_output_tokens',
  'total_cost_usd',
  'estimated_ratio',
  'estimated_call_count',
  'cost_coverage_ratio',
  'error_call_count',
  'record_failed',
  'by_node',
  'tool_context_saving',
] as const satisfies readonly LedgerField[];

/**
 * **不是独立读数、而是另一个读数的单位**的字段。
 *
 * `currency` 印在 `total_cost_usd` 的符号位上（`$12.34`），不占自己的格 ——
 * 把单位拆成第二枚读数，读者要在两个地方各看一眼才知道那是多少钱。
 */
export const UNIT_LEDGER_FIELDS: Record<string, string> = {
  currency: '作为 total_cost_usd 的符号印在同一个读数里（$ / ¥ / €），不占自己的格。',
};

/**
 * **刻意不上屏**的字段。每条一个理由 —— 这张表和上面那张一样是判据的一半：
 * 一个字段不许两张表都不在，也不许两张表都在。
 */
export const OFF_SCREEN_LEDGER_FIELDS: Record<string, string> = {
  run_id:
    '内部键。2026-07-24 的用户裁决（用户前台信息边界 §5）明写「run_id 不得出现在用户文本、' +
    '卡片、弹窗、错误、历史条目或用户可见 SSE 模型中」—— 这一条不在「把成本台账接回来」的' +
    '推翻范围内，所以它只用来对齐 run（哪份台账属于哪条消息），一个字都不印。',
  wall_ms:
    '**已经在屏幕上了** —— 思维链折叠头的定格总耗时就是它（`MessageBubble` 把它当 ' +
    '`serverTotalMs` 传下去）。同一个数在两处各印一遍，读者第一反应是「这两个为什么不一样」。' +
    '一个数一个家。',
  total_latency_ms:
    '逐次调用延迟的**求和**，与 wall_ms 回答同一个问题（这轮花了多久）而口径不同：' +
    '并发跑的两次调用在它里面加两遍，在 wall_ms 里只占一段。两个都印等于请读者' +
    '自己去猜哪个是真的。per-node 的那一份仍在「按环节」行里 —— 那里它回答的是' +
    '「哪个环节慢」，不是「这轮多久」。',
  by_agent:
    '与 by_node **同一份数据**：`run_control.py:513-514` 的节点包装器把 `current_agent` ' +
    '设成节点名本身，所以除了外层那个 `workflow` 兜底，两个分组逐行相同。印两份分解就是' +
    '「同一份数据有两个分组方式」的呈现版。上屏的那一份取 by_node —— 合同 §7.1 说的是「per-node 成本分解」。',
  bottleneck_by_cost:
    '后端从 by_node 取的成本 top3。by_node 已经按成本降序上屏并画了条形，' +
    '最贵的那个环节就在第一行 —— 再开一段「瓶颈 Top 3」是把同一个事实说第二遍。',
  bottleneck_by_latency:
    '同上，延迟维度的 top3。按环节那几行行尾已经带各自的耗时。',
  priced_call_count:
    '与 cost_coverage_ratio 同一个事实（有价调用占比）。比率上屏，因为它直接回答' +
    '「这个成本数覆盖了多少」；再印一个分子，读者得自己做除法。',
  unpriced_call_count: '同上，是 call_count − priced_call_count 的另一种说法。',
};

// ---------------------------------------------------------------------------
// 视图模型
// ---------------------------------------------------------------------------

export type CostLedgerSource = 'settled' | 'live';

/** 读数瓦片。`value` 给 `RollingNumber` 滚，`display` 是它滚出来的那串字。 */
export interface CostLedgerTile {
  field: LedgerField;
  label: string;
  /** 原始数值；`null` 表示无值（`display` 是占位符，不滚动）。 */
  value: number | null;
  display: string;
  /** 带 `≈` 前缀（§1：估算值与后端 `estimated` 标记一一对应）。 */
  estimated: boolean;
  accent: boolean;
}

/** 明细键值行（`ui/InspectHint` 的 `InspectRow` 声部）。 */
export interface CostLedgerRow {
  field: LedgerField;
  label: string;
  display: string;
}

/** 按环节一行。`barShare` 是条宽，**不是台账里的数**，不以数字形式印出来。 */
export interface CostLedgerBreakdownRow {
  key: string;
  label: string;
  tokens: string;
  cost: string;
  calls: string;
  latency: string;
  barShare: number;
}

export interface CostLedgerBreakdown {
  field: LedgerField;
  rows: CostLedgerBreakdownRow[];
  /** 未列出的环节数（折叠，不是截断）。 */
  rest: number;
}

export interface CostLedgerSaving {
  field: LedgerField;
  ratio: string;
  savedTokens: string;
  modeLabel: string;
  exposure: string;
}

export type CostLedgerNoticeTone = 'warning' | 'neutral';

export interface CostLedgerNotice {
  field: LedgerField | 'currency';
  tone: CostLedgerNoticeTone;
  text: string;
}

export interface CostLedgerView {
  source: CostLedgerSource;
  tiles: CostLedgerTile[];
  rows: CostLedgerRow[];
  breakdown: CostLedgerBreakdown | null;
  saving: CostLedgerSaving | null;
  notices: CostLedgerNotice[];
  /** 本轮至少有一次调用的 token 是估算的 → 读数带 `≈`。 */
  estimated: boolean;
  /** live 档下当前正在跑的环节（用户语言）；settled 档为 null。 */
  activeLabel: string | null;
}

// ---------------------------------------------------------------------------
// 投影
// ---------------------------------------------------------------------------

/** 按环节最多列这么多行；其余走「+ 其余 N 个环节」。折叠不是截断。 */
const BREAKDOWN_ROW_LIMIT = 5;

function nodeLabel(row: CostGroupBreakdown): string {
  const key = row.node ?? row.agent ?? '';
  if (!key || key === 'unknown') return '未归因调用';
  if (key === 'workflow') return '图边界调用';
  return formatAgentLabel(key).label;
}

function breakdownKey(row: CostGroupBreakdown, index: number): string {
  return `${row.node ?? row.agent ?? 'unknown'}#${index}`;
}

/**
 * 条宽（0..1）。**这是几何，不是数据**：它由「本段里最大的那个 total_tokens」定标，
 * 而 token 数本身原样印在行尾。此前那版把 `cost_usd / total_cost_usd` 当百分数印出来 ——
 * 一个后端没算过的比率，用户读到的是一个没人负责的数。
 */
function barShares(rows: CostGroupBreakdown[]): number[] {
  const max = rows.reduce((peak, row) => Math.max(peak, row.total_tokens), 0);
  if (max <= 0) return rows.map(() => 0);
  return rows.map((row) => row.total_tokens / max);
}

function savingModeLabel(mode: ToolContextSaving['mode']): string {
  switch (mode) {
    case 'deferred':
      return '按需加载';
    case 'mixed':
      return '混合加载';
    default:
      return '全部加载';
  }
}

function projectSettled(summary: RunCostSummary): CostLedgerView {
  const estimated = summary.estimated_call_count > 0;
  const costKnown = summary.total_cost_usd != null;

  const tiles: CostLedgerTile[] = [
    {
      field: 'call_count',
      label: '模型调用',
      value: summary.call_count,
      display: formatCount(summary.call_count),
      estimated: false,
      accent: false,
    },
    {
      field: 'total_tokens',
      label: 'Token',
      value: summary.total_tokens,
      display: formatTokensCompact(summary.total_tokens),
      estimated,
      accent: false,
    },
    {
      field: 'total_cost_usd',
      // 单位（`currency`）印在这个读数的符号位上，不占自己的格。
      label: '成本',
      value: summary.total_cost_usd,
      display: formatCost(summary.total_cost_usd, summary.currency),
      // 无值时不带 `≈`：占位符不是一个可以「大约」的数（`≈—` 什么也没说）。
      estimated: estimated && summary.total_cost_usd != null,
      accent: true,
    },
    {
      field: 'estimated_ratio',
      label: '估算占比',
      // 比率由后端算（`summarize_calls`）；这里连除号都没有。
      value: summary.estimated_ratio,
      display: formatPercent(summary.estimated_ratio),
      estimated: false,
      accent: false,
    },
  ];

  const rows: CostLedgerRow[] = [
    {
      field: 'total_input_tokens',
      label: '输入',
      display: `${formatTokens(summary.total_input_tokens)} tok`,
    },
    {
      field: 'total_output_tokens',
      label: '输出',
      display: `${formatTokens(summary.total_output_tokens)} tok`,
    },
    {
      field: 'total_cached_input_tokens',
      label: '缓存命中',
      display: `${formatTokens(summary.total_cached_input_tokens)} tok`,
    },
    {
      field: 'total_reasoning_output_tokens',
      label: '推理输出',
      display: `${formatTokens(summary.total_reasoning_output_tokens)} tok`,
    },
    {
      field: 'cost_coverage_ratio',
      label: '价格覆盖',
      display: formatPercent(summary.cost_coverage_ratio),
    },
    {
      field: 'estimated_call_count',
      label: '估算调用',
      // 分母是同一份台账里的 call_count，两个数各自原样印 —— 这里不做除法，
      // 除法的结果是上面那枚 `estimated_ratio` 瓦片，由后端算。
      display: `${formatCount(summary.estimated_call_count)} / ${formatCount(summary.call_count)}`,
    },
    {
      field: 'error_call_count',
      label: '出错调用',
      display: formatCount(summary.error_call_count),
    },
  ];

  const nodes = summary.by_node.slice(0, BREAKDOWN_ROW_LIMIT);
  const shares = barShares(nodes);
  const breakdown: CostLedgerBreakdown | null = summary.by_node.length
    ? {
        field: 'by_node',
        rows: nodes.map((row, index) => ({
          key: breakdownKey(row, index),
          label: nodeLabel(row),
          tokens: `${formatTokens(row.total_tokens)} tok`,
          cost: formatCost(row.cost_usd, summary.currency),
          calls: formatCount(row.call_count),
          latency: formatDuration(row.latency_ms),
          barShare: shares[index],
        })),
        rest: summary.by_node.length - nodes.length,
      }
    : null;

  const saving = summary.tool_context_saving
    ? {
        field: 'tool_context_saving' as LedgerField,
        ratio: formatPercent(summary.tool_context_saving.tool_context_saving),
        savedTokens: `${formatTokens(summary.tool_context_saving.schema_tokens_saved)} tok`,
        modeLabel: savingModeLabel(summary.tool_context_saving.mode),
        exposure: `${formatCount(summary.tool_context_saving.tools_exposed_initial)}/${formatCount(
          summary.tool_context_saving.tools_full_baseline
        )}`,
      }
    : null;

  const notices: CostLedgerNotice[] = [];
  if (summary.record_failed) {
    // CB-02：终结时落库失败已回放待重试。台账可能不完整这件事如实说，不静默吞账。
    notices.push({
      field: 'record_failed',
      tone: 'warning',
      text: `有 ${formatCount(summary.record_failed)} 条调用的成本正在补记，稍后自动更新。`,
    });
  }
  if (!costKnown && summary.call_count > 0) {
    notices.push({
      field: 'currency',
      tone: 'warning',
      text: '本轮未匹配到价目表，这里只报 token 用量。',
    });
  }

  return {
    source: 'settled',
    tiles,
    rows,
    breakdown,
    saving,
    notices,
    estimated,
    activeLabel: null,
  };
}

function projectLive(live: RunCostLive): CostLedgerView {
  const estimated = live.estimatedCount > 0;

  const tiles: CostLedgerTile[] = [
    {
      field: 'call_count',
      label: '模型调用',
      value: live.callCount,
      display: formatCount(live.callCount),
      estimated: false,
      accent: false,
    },
    {
      field: 'total_tokens',
      label: 'Token',
      value: live.totalTokens,
      display: formatTokensCompact(live.totalTokens),
      estimated,
      accent: false,
    },
    {
      field: 'total_cost_usd',
      label: '成本',
      value: live.costKnown ? live.totalCostUsd : null,
      // `costKnown === false` 时报占位符，不报 `$0.00`：一次都没命中价目表不等于没花钱。
      display: live.costKnown ? formatCost(live.totalCostUsd, 'USD') : READOUT_PLACEHOLDER,
      estimated: estimated && live.costKnown,
      accent: true,
    },
  ];

  const rows: CostLedgerRow[] = [
    {
      field: 'total_input_tokens',
      label: '输入',
      display: `${formatTokens(live.totalInputTokens)} tok`,
    },
    {
      field: 'total_output_tokens',
      label: '输出',
      display: `${formatTokens(live.totalOutputTokens)} tok`,
    },
    {
      field: 'estimated_call_count',
      label: '估算调用',
      display: `${formatCount(live.estimatedCount)} / ${formatCount(live.callCount)}`,
    },
  ];

  // **运行中没有 `estimated_ratio` / `cost_coverage_ratio` 这两枚瓦片。**
  // 它们是后端在 `summarize_calls` 里算的，run 没终结就还没算过。拿
  // `estimatedCount / callCount` 在这里补一个出来，就是同一个数的第二个产地 ——
  // 而且它和终结后那个值会不一样（后端算的分母是落库的全部调用，包含并发 run
  // 之外这条流没收到的那些补发）。少一枚瓦片，不许自己算。

  const notices: CostLedgerNotice[] = [];
  if (!live.costKnown && live.callCount > 0) {
    notices.push({
      field: 'currency',
      tone: 'warning',
      text: '尚未匹配到价目表，暂时只累计 token。',
    });
  }

  const activeInternal = live.lastAgent ?? live.lastNode;

  return {
    source: 'live',
    tiles,
    rows,
    breakdown: null,
    saving: null,
    notices,
    estimated,
    activeLabel: activeInternal ? formatAgentLabel(activeInternal).label : null,
  };
}

/**
 * 台账 → 上屏视图。没有台账就返回 `null` —— **没有数据就没有印记**（§1）。
 *
 * `run_cost_summary` 在场即整块接管（终结值权威）；否则用运行中累加器。
 * 一次调用都没有、也没有落库失败的一轮不算「有台账」：那时印记不该出现。
 */
export function buildCostLedgerView(
  summary: RunCostSummary | null | undefined,
  live: RunCostLive | null | undefined
): CostLedgerView | null {
  if (summary && (summary.call_count > 0 || summary.record_failed)) {
    return projectSettled(summary);
  }
  if (live && live.callCount > 0) {
    return projectLive(live);
  }
  return null;
}

/** 悬停微释义（§3.3 那一句「本轮真实成本台账」的产品措辞）。 */
export function costLedgerBlurb(view: CostLedgerView): string {
  return view.source === 'live' ? '这轮的模型用量正在累计' : '这轮的模型用量与成本台账';
}

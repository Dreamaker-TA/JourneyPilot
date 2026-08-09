import type {
  ContextReport,
  ContextReportCompaction,
  ContextReportSection,
  ContextReportSectionKey,
} from '../../types/chat';

/**
 * 上下文透镜数据规范化。
 *
 * SSE 实时分支与会话历史加载共用这一入口，保证两条来源一致。
 *
 * **这里不做业务截断。** 此前这一行是 `.slice(0, 8)`：后端印进 prompt 的是三段各自的
 * 预算（12 / 24 / 6），前端摊平后砍回 8 条 —— 于是「后端放宽了条数」这件事在界面上
 * 完全不可见，缺陷可以在后端全绿的情况下继续骗人。装了多少就列多少，条数由后端预算层
 * 说了算，说一次。
 */

const SECTION_KEYS: readonly ContextReportSectionKey[] = ['hard', 'preference', 'reference'];

function normalizeCompaction(raw: unknown): ContextReportCompaction {
  return { triggered: Boolean(raw && typeof raw === 'object' && (raw as Record<string, unknown>).triggered) };
}

/**
 * 一段的规范化：段名不在合同里就返回 null，由调用方把**整份**报告作废。
 * 悄悄吞掉一段等于把「这几条参考信息存在过」这件事从界面上抹掉，那正是这枚缺陷本身。
 */
function normalizeSection(raw: unknown): ContextReportSection | null {
  if (!raw || typeof raw !== 'object') return null;
  const row = raw as Record<string, unknown>;
  const key = SECTION_KEYS.find((candidate) => candidate === row.key);
  if (!key) return null;
  const items = Array.isArray(row.items)
    ? row.items.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
    : [];
  return { key, items };
}

/** snake_case 原始载荷 → 用户可读的三段参考信息。 */
export function normalizeContextReport(raw: unknown): ContextReport | null {
  if (!raw || typeof raw !== 'object') return null;
  const row = raw as Record<string, unknown>;
  // 换形状之前落库的 context_report 事件没有这个键。本仓不设兼容层：老事件就没有印记，
  // 而不是在这里补一个默认值把半份旧报告画成一份新报告。
  if (!Array.isArray(row.referenced_sections)) return null;

  const sections: ContextReportSection[] = [];
  for (const entry of row.referenced_sections) {
    const section = normalizeSection(entry);
    if (!section) return null;
    if (section.items.length > 0) sections.push(section);
  }
  return { sections, compaction: normalizeCompaction(row.compaction) };
}

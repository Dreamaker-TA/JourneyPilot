import type { ChatSession } from '../types/chat';

type ClassValue = string | number | false | null | undefined | Record<string, boolean>;

/** 轻量版 classnames：拼接条件 class，过滤假值 */
export function cn(...classes: ClassValue[]): string {
  const out: string[] = [];
  for (const c of classes) {
    if (!c) continue;
    if (typeof c === 'string' || typeof c === 'number') {
      out.push(String(c));
    } else {
      for (const [key, val] of Object.entries(c)) {
        if (val) out.push(key);
      }
    }
  }
  return out.join(' ');
}

let idCounter = 0;

/** 生成本地唯一 id（时间戳 + 随机段 + 自增计数） */
export function generateId(): string {
  idCounter += 1;
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}${idCounter.toString(36)}`;
}

/** 截断字符串，超长时追加省略号 */
export function truncate(str: string, maxLength: number): string {
  if (!str) return '';
  return str.length > maxLength ? `${str.slice(0, maxLength)}…` : str;
}

/** 会话列表按更新时间分组：今天 / 昨天 / 近 7 天 / 更早 */
export function groupByDate(sessions: ChatSession[]): Map<string, ChatSession[]> {
  const groups = new Map<string, ChatSession[]>();
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000;
  const startOfWeek = startOfToday - 6 * 24 * 60 * 60 * 1000;

  const sorted = [...sessions].sort(
    (a, b) => b.updatedAt.getTime() - a.updatedAt.getTime()
  );

  for (const session of sorted) {
    const t = session.updatedAt.getTime();
    let label: string;
    if (t >= startOfToday) label = '今天';
    else if (t >= startOfYesterday) label = '昨天';
    else if (t >= startOfWeek) label = '近 7 天';
    else label = '更早';

    const list = groups.get(label);
    if (list) list.push(session);
    else groups.set(label, [session]);
  }

  return groups;
}

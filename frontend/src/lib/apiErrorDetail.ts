import { ApiError } from './api';

/**
 * 机读错误 detail 的唯一读取器。
 *
 * 后端的 `detail` 有两种形状：一句字符串，和 `{code, message, …}`（trip_runs.py 13 处 +
 * chat.py 1 处 + places.py 2 处）。只有后者能被判据消费——`code` 是给代码看的、`message`
 * 是给人看的。读不到就返回两个 null，调用方据此回落到自己的兜底文案，而不是去猜字符串。
 *
 * 这三层展开（是不是 ApiError → body 是不是 object → detail 是不是 object）**只写在这里**：
 * 抄两遍的东西迟早只改一处。
 */
export interface ApiErrorDetail {
  code: string | null;
  message: string | null;
}

function detailRecord(error: unknown): Record<string, unknown> | null {
  if (!(error instanceof ApiError) || !error.body || typeof error.body !== 'object') return null;
  const detail = (error.body as Record<string, unknown>).detail;
  if (!detail || typeof detail !== 'object') return null;
  return detail as Record<string, unknown>;
}

export function apiErrorDetail(error: unknown): ApiErrorDetail {
  const record = detailRecord(error);
  if (!record) return { code: null, message: null };
  return {
    code: typeof record.code === 'string' ? record.code : null,
    message: typeof record.message === 'string' ? record.message : null,
  };
}

/**
 * detail 里 `code`/`message` 之外的具名字符串字段，例如冲突回报的
 * `current_bundle_id`。空串按缺失处理：一个空 id 不是答案。
 */
export function apiErrorDetailString(error: unknown, key: string): string | null {
  const value = detailRecord(error)?.[key];
  return typeof value === 'string' && value ? value : null;
}

import type { ContextCompactionEvent } from '../types/chat';

/**
 * Convert persisted/API snake_case compression payloads into the UI contract.
 *
 * `messages_compressed` / `tokens_before` / `tokens_after` are **not** mapped.
 * The notice never printed them — the ⓘ surface shows no
 * token counts — so carrying them here produced three numbers that existed only
 * to be dropped. They stay on the durable record (`trip_run_events` / the
 * persisted turn), which is where observability lives.
 */
export function normalizeContextCompactionEvent(raw: unknown): ContextCompactionEvent | null {
  if (!raw || typeof raw !== 'object') return null;
  const row = raw as Record<string, unknown>;
  const id = typeof row.event_id === 'string' ? row.event_id.trim() : '';
  const source = row.source;
  const occurredAt = typeof row.occurred_at === 'string' ? row.occurred_at.trim() : '';
  if (!id || (source !== 'manual' && source !== 'automatic') || !occurredAt) return null;

  return {
    id,
    source,
    occurredAt,
    summary: typeof row.summary === 'string' ? row.summary : '',
    keyConstraints: Array.isArray(row.key_constraints)
      ? row.key_constraints.map((item) => String(item).trim()).filter(Boolean)
      : [],
  };
}

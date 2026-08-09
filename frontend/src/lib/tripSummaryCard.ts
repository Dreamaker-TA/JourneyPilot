import type { TripSummaryCard, TripSummaryFact } from '../types/chat';

/**
 * Accept only the deliberately small server contract used by the research
 * summary card.  This keeps the card decoupled from raw workflow state.
 */
export function normalizeTripSummaryCard(
  raw: { summary_card?: Record<string, unknown> } | Record<string, unknown> | null | undefined
): TripSummaryCard | null {
  const card = raw && 'summary_card' in raw ? raw.summary_card : raw;
  if (!card || typeof card !== 'object') return null;

  const value = card as Record<string, unknown>;
  if (
    typeof value.headline !== 'string'
    || typeof value.summary !== 'string'
    || typeof value.current_focus !== 'string'
    || typeof value.compact_line !== 'string'
  ) {
    return null;
  }

  const facts: TripSummaryFact[] = Array.isArray(value.facts)
    ? value.facts.flatMap((fact): TripSummaryFact[] => {
        if (!fact || typeof fact !== 'object') return [];
        const row = fact as Record<string, unknown>;
        if (typeof row.label !== 'string' || typeof row.value !== 'string') return [];
        if (row.state !== 'confirmed' && row.state !== 'default' && row.state !== 'deferred') return [];
        return [{ label: row.label, value: row.value, state: row.state }];
      })
    : [];

  return {
    headline: value.headline,
    summary: value.summary,
    facts,
    priorities: Array.isArray(value.priorities)
      ? value.priorities.filter((item): item is string => typeof item === 'string')
      : [],
    currentFocus: value.current_focus,
    nextMilestone: typeof value.next_milestone === 'string' ? value.next_milestone : null,
    compactLine: value.compact_line,
    requiresUserConfirmation: value.requires_user_confirmation === true,
  };
}

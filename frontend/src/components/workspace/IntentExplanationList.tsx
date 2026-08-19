import type { EntityIntentExplanation } from '../../types/delivery';

export function IntentExplanationList({ items }: { items: readonly EntityIntentExplanation[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="mt-2 space-y-1 border-l-2 border-accent/30 pl-3" data-testid="intent-explanations">
      {items.map((item) => (
        <li key={`${item.label}:${item.explanation}`} className="break-words text-xs leading-5 text-ink-secondary">
          <strong className="font-semibold text-ink">{item.label}：</strong>{item.explanation}
        </li>
      ))}
    </ul>
  );
}

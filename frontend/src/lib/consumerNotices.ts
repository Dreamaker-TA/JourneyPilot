import type { PublicDeliveryBundle, PublicWeatherAdjustment } from '../types/delivery';

export type ConsumerNoticeSeverity = 'attention' | 'blocking';
export type ConsumerNoticeScope = 'trip' | 'day' | 'entity';

export interface ConsumerNotice {
  key: string;
  severity: ConsumerNoticeSeverity;
  scope: ConsumerNoticeScope;
  dayId?: string;
  entityId?: string;
  title: string;
  detail?: string;
  actionLabel?: string;
}

export interface ConsumerNoticeLoadState {
  status: 'idle' | 'loading' | 'ready' | 'error';
  message: string | null;
}

interface WeatherNoticeGroup {
  scope: ConsumerNoticeScope;
  dayId?: string;
  entityId: string;
  proposals: PublicWeatherAdjustment[];
}

/**
 * Stable identity for a consumer-visible issue. Revisions deliberately take
 * part in the key: a dismissed or handled notice stays gone until the current
 * Delivery Bundle changes the underlying fact.
 */
export function consumerNoticeKey(
  kind: string,
  scope: ConsumerNoticeScope,
  entityOrDay: string,
  revision: number | string,
): string {
  return `${kind}:${scope}:${entityOrDay}:${revision}`;
}

function dedupeConsumerNotices(notices: ConsumerNotice[]): ConsumerNotice[] {
  return [...new Map(notices.map((notice) => [notice.key, notice])).values()];
}

function weatherNoticeGroups(bundle: PublicDeliveryBundle): WeatherNoticeGroup[] {
  const dayIdByDate = new Map(
    bundle.workspace.itinerary.day_plans
      .filter((day) => Boolean(day.date))
      .map((day) => [day.date!, day.day_id]),
  );
  const groups = new Map<string, WeatherNoticeGroup>();

  for (const proposal of bundle.workspace.weather_adjustments) {
    if (proposal.status !== 'pending') continue;

    const dayId = dayIdByDate.get(proposal.date);
    const scope: ConsumerNoticeScope = dayId ? 'day' : 'trip';
    const entityId = dayId || proposal.proposal_id;
    const groupKey = `${scope}:${entityId}`;
    const existing = groups.get(groupKey);

    if (existing) {
      existing.proposals.push(proposal);
    } else {
      groups.set(groupKey, {
        scope,
        ...(dayId ? { dayId } : {}),
        entityId,
        proposals: [proposal],
      });
    }
  }

  return [...groups.values()];
}

/**
 * Projects only current Delivery Bundle facts and formal load state into the
 * consumer reminder layer. Tool/provider/audit events are intentionally not
 * accepted here: successful execution fallback is an inspection fact, not a
 * traveller warning.
 */
export function buildConsumerNotices(
  bundle: PublicDeliveryBundle | null,
  loadState: ConsumerNoticeLoadState,
): ConsumerNotice[] {
  if (loadState.status === 'error') {
    const revision = bundle?.manifest.workspace_revision ?? 'unavailable';
    return [{
      key: consumerNoticeKey('delivery_load_failure', 'trip', 'current-delivery', revision),
      severity: 'blocking',
      scope: 'trip',
      title: '暂时无法加载行程',
      detail: '请稍后重试。',
      actionLabel: '重试',
    }];
  }

  if (!bundle) return [];

  const revision = bundle.manifest.workspace_revision;
  const notices = weatherNoticeGroups(bundle).map((group) => {
    const [firstProposal] = group.proposals;
    const count = group.proposals.length;
    const isDayNotice = group.scope === 'day';

    return {
      key: consumerNoticeKey('weather_adjustment', group.scope, group.entityId, revision),
      severity: 'attention' as const,
      scope: group.scope,
      ...(group.dayId ? { dayId: group.dayId } : {}),
      title: isDayNotice
        ? count === 1
          ? '天气可能影响当天安排'
          : `${count} 项天气变化可能影响当天安排`
        : count === 1
          ? '天气可能影响行程安排'
          : `${count} 项天气变化可能影响行程安排`,
      ...(count === 1 && firstProposal.summary ? { detail: firstProposal.summary } : {}),
      actionLabel: '查看调整建议',
    };
  });

  return dedupeConsumerNotices(notices);
}

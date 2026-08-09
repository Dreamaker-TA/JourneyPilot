import type { TripReportProjection } from '../types/delivery';

/**
 * Weather is deliberately rendered from the report projection.  The raw
 * weather ledger also contains coverage, alerts, impact targets, and provider
 * details that are useful for execution diagnostics but are not a product UI
 * contract.
 */
export type PublicWeatherDay = NonNullable<TripReportProjection['document']>['weather'][number];

export type WeatherIconKind =
  | 'clear'
  | 'partly'
  | 'cloudy'
  | 'rain'
  | 'drizzle'
  | 'thunder'
  | 'snow'
  | 'fog'
  | 'wind'
  | 'unknown';

export interface WeatherDayVM extends PublicWeatherDay {
  icon: WeatherIconKind;
}

export interface WeatherDestinationGroup {
  destinationId: string;
  label: string;
  days: WeatherDayVM[];
}

export function isRainy(icon: WeatherIconKind): boolean {
  return icon === 'rain' || icon === 'drizzle' || icon === 'thunder';
}

export function weatherIconForLabel(label: string | null): WeatherIconKind {
  if (!label) return 'unknown';
  const normalized = label.toLowerCase();
  if (/雷暴|雷雨|thunder|lightning/.test(normalized)) return 'thunder';
  if (/小雨|毛毛雨|drizzle/.test(normalized)) return 'drizzle';
  if (/降雨|阵雨|暴雨|雨|rain/.test(normalized)) return 'rain';
  if (/降雪|雨夹雪|雪|snow/.test(normalized)) return 'snow';
  if (/雾|霾|fog|haze/.test(normalized)) return 'fog';
  if (/大风|强风|wind/.test(normalized)) return 'wind';
  if (/多云|partly/.test(normalized)) return 'partly';
  if (/阴|cloud/.test(normalized)) return 'cloudy';
  if (/晴|sun|clear/.test(normalized)) return 'clear';
  return 'unknown';
}

function toViewModel(day: PublicWeatherDay): WeatherDayVM {
  return { ...day, icon: weatherIconForLabel(day.condition_label) };
}

/**
 * Only qualified forecast or seasonal facts enter the consumer overview.
 * Unavailable coverage remains an internal, typed absence instead of UI noise.
 */
export function buildWeatherGroups(days: readonly PublicWeatherDay[]): WeatherDestinationGroup[] {
  const grouped = new Map<string, { label: string; days: WeatherDayVM[] }>();
  for (const day of days) {
    if (day.data_kind === 'unavailable') continue;
    const existing = grouped.get(day.destination_id);
    if (existing && existing.label !== day.destination_name) {
      throw new Error(`Conflicting destination name for ${day.destination_id}`);
    }
    const group = existing ?? { label: day.destination_name, days: [] };
    group.days.push(toViewModel(day));
    grouped.set(day.destination_id, group);
  }
  return [...grouped.entries()].map(([destinationId, group]) => ({
    destinationId,
    label: group.label,
    days: group.days.sort((left, right) => left.date.localeCompare(right.date)),
  }));
}

export function buildWeatherByDestinationDate(
  days: readonly PublicWeatherDay[],
): Map<string, WeatherDayVM> {
  const result = new Map<string, WeatherDayVM>();
  for (const day of days) {
    if (day.data_kind === 'unavailable') continue;
    result.set(`${day.destination_id}:${day.date}`, toViewModel(day));
  }
  return result;
}

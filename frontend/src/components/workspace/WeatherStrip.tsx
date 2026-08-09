import React from 'react';
import {
  CalendarRange,
  Clock,
  Cloud,
  CloudDrizzle,
  CloudFog,
  CloudLightning,
  CloudRain,
  CloudSnow,
  CloudSun,
  Droplets,
  History,
  Sun,
  Wind,
  type LucideProps,
} from 'lucide-react';
import { weatherFreshnessText } from '../../lib/deliveryLabels';
import type { DayWeatherVM } from '../../lib/itineraryPresentation';
import { isRainy, type WeatherIconKind } from '../../lib/weatherViewModel';

const ICONS: Record<WeatherIconKind, React.ComponentType<LucideProps>> = {
  clear: Sun,
  partly: CloudSun,
  cloudy: Cloud,
  rain: CloudRain,
  drizzle: CloudDrizzle,
  thunder: CloudLightning,
  snow: CloudSnow,
  fog: CloudFog,
  wind: Wind,
  unknown: Cloud,
};

export const WeatherIcon: React.FC<{ kind: WeatherIconKind } & LucideProps> = ({ kind, ...props }) => {
  const Icon = ICONS[kind] ?? Cloud;
  return <Icon {...props} />;
};

/**
 * 时效角标 —— 每天只出现一次，点开是两个态之一：这份读数什么时候测的，或者它根本
 * 不是这一天的读数。
 *
 * 没有它，一个连着几天刷新失败的 run 和一分钟前刚刷过的 run 在界面上长得一模一样。二态由
 * 投影期判定（`services/delivery_projection.py`），文案与后端逐字同一份，PDF 打印同一句。
 *
 * 刻意不出「本次未能刷新天气」这类防御性文案：那和「读数更旧」是同一件事，说两遍会让
 * 读者把其中一句当成报错。
 */
const WeatherFreshnessBadge: React.FC<{ weather: DayWeatherVM }> = ({ weather }) => {
  const [open, setOpen] = React.useState(false);
  const text = weatherFreshnessText(weather.dataState, weather.observedAt);
  if (!text) return null;
  const historical = weather.dataState === 'historical';
  return (
    <span className="inline-flex shrink-0 items-center">
      <button
        type="button"
        data-testid="weather-freshness-badge"
        data-weather-data-state={weather.dataState}
        aria-expanded={open}
        aria-label="天气数据时效"
        onClick={() => setOpen((current) => !current)}
        className="inline-flex min-h-6 items-center rounded-card px-1 text-ink-muted transition-colors hover:text-ink"
      >
        {historical ? <History size={11} aria-hidden /> : <Clock size={11} aria-hidden />}
      </button>
      {open && (
        <span data-testid="weather-freshness-detail" className="ml-1 break-words text-[11px] font-normal text-ink-muted">
          {text}
        </span>
      )}
    </span>
  );
};

/** A compact weather context for a current, canonical day only. */
export const DayWeatherBadge: React.FC<{ weather: DayWeatherVM }> = ({ weather }) => {
  if (weather.dataKind === 'seasonal_baseline') {
    return (
      <span data-testid="day-weather-badge" className="inline-flex min-h-7 max-w-full items-center gap-1.5 rounded-label bg-surface px-2.5 py-1 text-[11px] font-medium text-ink-secondary">
        <CalendarRange size={13} className="shrink-0 text-chart" aria-hidden />
        <span className="break-words">季节参考{weather.temperatureLabel ? ` · 常见 ${weather.temperatureLabel}` : ''}</span>
        <WeatherFreshnessBadge weather={weather} />
      </span>
    );
  }

  return (
    <span data-testid="day-weather-badge" className="inline-flex min-h-7 max-w-full items-center gap-1.5 rounded-label bg-surface px-2.5 py-1 text-[11px] font-medium text-ink-secondary">
      <WeatherIcon kind={weather.icon} size={14} className="shrink-0 text-accent" aria-hidden />
      {weather.conditionLabel && <span className="break-words">{weather.conditionLabel}</span>}
      {weather.temperatureLabel && <span className="shrink-0 tabular-nums text-ink">{weather.temperatureLabel}</span>}
      {weather.precipitationProbabilityPct != null && isRainy(weather.icon) && (
        <span className="inline-flex shrink-0 items-center gap-0.5 text-chart">
          <Droplets size={11} aria-hidden />
          <span className="tabular-nums">{weather.precipitationProbabilityPct}%</span>
        </span>
      )}
      <WeatherFreshnessBadge weather={weather} />
    </span>
  );
};

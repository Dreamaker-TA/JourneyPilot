import React from 'react';
import { ArrowUpRight, CalendarRange, Droplets } from 'lucide-react';
import { WEATHER_HISTORICAL_LABEL } from '../../lib/deliveryLabels';
import type { DaySummaryVM } from '../../lib/itineraryPresentation';
import { isRainy } from '../../lib/weatherViewModel';
import { DayOrdinalChip } from './DayOrdinalChip';
import { TimelineNodeMark } from './TimelineNodeMark';
import { WeatherIcon } from './WeatherStrip';

interface DaySummaryCardProps {
  day: DaySummaryVM;
  onOpen: () => void;
  cardRef?: React.Ref<HTMLButtonElement>;
}

function DayWeather({ weather }: { weather: NonNullable<DaySummaryVM['weather']> }) {
  const seasonal = weather.dataKind === 'seasonal_baseline';
  return (
    <span data-testid="day-weather-badge" className="inline-flex min-h-7 max-w-full items-center gap-1.5 text-[11px] font-medium text-ink-secondary">
      {seasonal
        ? <CalendarRange size={14} className="shrink-0 text-chart" aria-hidden />
        : <WeatherIcon kind={weather.icon} size={14} className="shrink-0 text-accent" aria-hidden />}
      <span className="min-w-0 break-words">
        {seasonal ? '季节参考' : weather.conditionLabel}
        {weather.temperatureLabel ? `${seasonal ? ' · 常见 ' : ' · '}${weather.temperatureLabel}` : ''}
      </span>
      {!seasonal && weather.precipitationProbabilityPct != null && isRainy(weather.icon) && (
        <span className="inline-flex shrink-0 items-center gap-0.5 tabular-nums text-chart">
          <Droplets size={11} aria-hidden />
          {weather.precipitationProbabilityPct}%
        </span>
      )}
      {/* 总览卡整张是一个按钮，所以这里不能嵌一个可点开的角标（HTML 不允许，也会
          抢掉卡片本身的点击）。只印会改变读法的那一个态：「这不是这一天的读数」。
          刷新时间不改变读法，留给日头里的角标。文案与后端逐字同一份。 */}
      {weather.dataState === 'historical' && (
        <span data-testid="day-weather-historical" className="shrink-0 text-ink-muted">{WEATHER_HISTORICAL_LABEL}</span>
      )}
    </span>
  );
}

export const DaySummaryCard: React.FC<DaySummaryCardProps> = ({ day, onOpen, cardRef }) => {
  const metadata = [day.dateLabel, day.weekdayLabel, day.destinationLabel].filter((value): value is string => Boolean(value));
  const accessibleLabel = [`查看第 ${day.day} 天`, ...metadata, day.theme].filter(Boolean).join('：');

  return (
    <button
      ref={cardRef}
      type="button"
      data-testid={`day-summary-card-${day.dayId}`}
      aria-label={accessibleLabel}
      onClick={onOpen}
      /* 悬停不抬升、不加阴影：校准表在「克制底线 / warm·claude」
         那一行明写「hover 不 scale/lift」，而一个会浮起来的方块读起来像可以拖走。全站可点
         的面（行程行、风格卡、这里）一律「边框着色 + 一层暖底」。 */
      className="group w-full rounded-card border border-stroke bg-panel px-4 py-4 text-left transition-colors duration-fast ease-standard hover:border-accent/40 hover:bg-accent/[0.02] sm:px-5 sm:py-5"
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-5 gap-y-2">
        <div className="min-w-0">
          {/* 日号与日头、与地图上这一天的路线同一个颜色、同一份实现。**不要**换成
              `text-accent`：那会让四天印四个一样的交互蓝，而地图上是四种颜色。 */}
          <DayOrdinalChip day={day.day} />
          {metadata.length > 0 && <p className="mt-1.5 break-words text-sm text-ink-secondary">{metadata.join(' · ')}</p>}
        </div>
        {day.weather && <DayWeather weather={day.weather} />}
      </div>

      <h3 className="mt-3 break-words text-base font-semibold leading-6 text-ink">{day.theme}</h3>

      {day.nodes.length > 0 && (
        <ol className="mt-4" aria-label={`第 ${day.day} 天核心安排`}>
          {day.nodes.map((node, index) => (
            <React.Fragment key={node.key}>
              <li className="grid min-w-0 grid-cols-[minmax(0,4.75rem)_minmax(0,1fr)] gap-x-3">
                {node.timeLabel
                  ? <time className="pt-0.5 text-xs font-semibold tabular-nums text-ink-secondary">{node.timeLabel}</time>
                  : <span aria-hidden />}
                <div className="min-w-0 pb-3">
                  {/* 域身份由这枚记号承担，与展开后的时间轴同一份实现。**不要**改成按 role
                      给标题上色：role 的「地点」一挡同时盖住景点与餐饮，总览这一屏正是用来
                      扫读的，两者归并就分不开了；`text-accent` 还会占用唯一的交互蓝。 */}
                  <div className="flex min-w-0 items-start gap-2">
                    <TimelineNodeMark node={node} ringed={false} className="mt-0.5" />
                    <p className="min-w-0 break-words text-sm font-medium leading-5 text-ink">{node.title}</p>
                  </div>
                  {/* 时长与费用同属一条安排的量化事实：总览掉价格、点进去又冒出来，会让人
                      以为是两份行程。与展开后的路线行读同一份 VM、同一个货币格式。 */}
                  {(node.durationLabel || node.priceLabel) && (
                    <p className="mt-0.5 pl-6 text-xs text-ink-muted">
                      {[node.durationLabel, node.priceLabel].filter(Boolean).join(' · ')}
                    </p>
                  )}
                </div>
              </li>
              {index < day.nodes.length - 1 && (
                <li aria-hidden className="grid grid-cols-[minmax(0,4.75rem)_minmax(0,1fr)] gap-x-3">
                  <span />
                  {/* 连接线对准记号的圆心：记号 16px 宽、从第二列起，圆心在 8px 处。 */}
                  <span className="mb-2 ml-[7.5px] block h-3 w-px bg-stroke" />
                </li>
              )}
            </React.Fragment>
          ))}
        </ol>
      )}

      {day.hiddenNodeCount > 0 && <p className="mt-1 text-xs text-ink-muted">另有 {day.hiddenNodeCount} 项安排</p>}
      {day.notice && <p data-testid={`day-summary-notice-${day.dayId}`} className="mt-3 break-words border-t border-stroke pt-3 text-xs font-medium leading-5 text-warning">{day.notice.title}</p>}

      <span className="mt-4 inline-flex items-center gap-1 text-sm font-semibold text-accent group-hover:text-[var(--color-accent-hover)]">
        查看详情
        <ArrowUpRight size={15} aria-hidden />
      </span>
    </button>
  );
};

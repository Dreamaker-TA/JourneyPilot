import type { DayWeatherVM } from '../../lib/itineraryPresentation';
import { DayOrdinalChip } from './DayOrdinalChip';
import { DayWeatherBadge } from './WeatherStrip';

export interface DayHeaderProps {
  dayId: string;
  day: number;
  dateLabel: string | null;
  weekdayLabel: string | null;
  destinationLabel: string | null;
  theme: string;
  weather: DayWeatherVM | null;
}

/**
 * The single, traveller-facing heading for a day.  It deliberately receives
 * the current view model rather than reconstructing dates, locations, or
 * weather from report prose.
 */
export function DayHeader({
  dayId,
  day,
  dateLabel,
  weekdayLabel,
  destinationLabel,
  theme,
  weather,
}: DayHeaderProps) {
  const tripMeta = [dateLabel, weekdayLabel, destinationLabel].filter(
    (value): value is string => Boolean(value),
  );

  return (
    /*
     * 日分隔是一条**登记带**：整块暖色底 + 边框把一天圈起来，日号进等宽读数声部。
     *
     * 当日色**不走任何一条边**：一条 2px 的当日色横规读起来就是一根选中态色条，而
     * anti-slop 禁的正是这个形态（禁 border-left 色条；换成
     * border-top 只是把同一根条子转了 90°）。第 1 天的分类色恰好是 `#007AFF`，那根横规
     * 于是还会和唯一的交互蓝撞脸，看着像「这一天被选中了」。
     *
     * 当日色只出现在**日号标签自己**这一枚小牌上：1.5px 当日色描边 + 8% 当日色底。这就是
     * 那条替代路径（整面着色 + 字重），只不过用在标签这一级；十个分类色里有黄
     * 有青，所以着色的是边与底，字始终是 ink——否则第 9 天的字会淡到读不出来。
     */
    <header
      data-testid={`day-header-${dayId}`}
      className="mb-5 min-w-0 overflow-hidden rounded-card border border-stroke bg-surface/55 px-4 py-3.5 sm:mb-6 sm:px-5"
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-5 gap-y-2.5">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
            {/* 日牌：当日色描边 + 淡底，颜色与地图上这一天的路线逐字同源。总览卡读的是
                同一份实现（`DayOrdinalChip`），两个面不再各写一份。 */}
            <DayOrdinalChip day={day} />
            {tripMeta.length > 0 && (
              <p className="min-w-0 break-words text-xs text-ink-secondary">{tripMeta.join(' · ')}</p>
            )}
          </div>
          <h2 id={`delivery-day-${dayId}`} className="mt-1.5 break-words text-xl font-semibold tracking-[-0.015em] text-ink">
            {theme}
          </h2>
        </div>
        {weather && <DayWeatherBadge weather={weather} />}
      </div>
    </header>
  );
}

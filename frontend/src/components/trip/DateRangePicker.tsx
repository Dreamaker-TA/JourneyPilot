import React from 'react';
import { CalendarDays, ChevronLeft, ChevronRight, X } from 'lucide-react';
import { cn } from '../../lib/utils';
import { FIELD_LABEL, FIELD_VALUE, RULED_FIELD } from '../ui/Input';

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];

function iso(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function fromIso(value: string): Date | null {
  if (!value) return null;
  const [year, month, day] = value.split('-').map(Number);
  return new Date(year, month - 1, day);
}

function addMonths(date: Date, amount: number): Date {
  return new Date(date.getFullYear(), date.getMonth() + amount, 1);
}

function daysBetween(start: string, end: string): number {
  const left = fromIso(start);
  const right = fromIso(end);
  if (!left || !right) return 0;
  return Math.floor((right.getTime() - left.getTime()) / 86_400_000) + 1;
}

const Month: React.FC<{
  month: Date;
  start: string;
  end: string;
  onSelect: (value: string) => void;
}> = ({ month, start, end, onSelect }) => {
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const count = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  const cells: Array<number | null> = Array(first.getDay()).fill(null);
  for (let day = 1; day <= count; day += 1) cells.push(day);
  while (cells.length % 7) cells.push(null);

  return (
    <section className="min-w-0 flex-1" aria-label={`${month.getFullYear()}年${month.getMonth() + 1}月`}>
      <h3 className="mb-3 text-center text-sm font-semibold text-ink">{month.getFullYear()} 年 {month.getMonth() + 1} 月</h3>
      <div className="grid grid-cols-7 gap-1">
        {WEEKDAYS.map((item) => <span key={item} className="py-1 text-center text-[11px] text-ink-secondary">{item}</span>)}
        {cells.map((day, index) => {
          if (!day) return <span key={`blank-${index}`} />;
          const value = iso(new Date(month.getFullYear(), month.getMonth(), day));
          const isStart = value === start;
          const isEnd = value === end && value !== start;
          const selected = isStart || isEnd;
          const inRange = Boolean(start && end && value > start && value < end);
          // 「返程」不是「返航」：这个产品里最常见的返程方式是高铁，航空词汇在一趟
          // 上海→杭州的日历上就是错的（§UX Copy：先用旅行者的话）。
          const caption = isStart ? '出发' : isEnd ? '返程' : '';
          return (
            <button
              key={value}
              type="button"
              aria-label={`选择 ${value}${caption ? `（${caption}）` : ''}`}
              aria-pressed={selected}
              onClick={() => onSelect(value)}
              className={cn(
                'flex aspect-square min-h-9 flex-col items-center justify-center rounded-card text-sm transition-colors',
                selected && 'bg-accent font-semibold text-white',
                inRange && 'bg-accent-soft text-accent',
                !selected && !inRange && 'text-ink hover:bg-surface',
              )}
            >
              {/* 12px 是类型表最小的一挡，**不要再往下压**：这里印的是两个汉字，而表里最小
                  的挡位之下没有任何一档是给中文的。`uppercase` 对中文是空操作，`tracking-wide`
                  只会在最后一个字后面加一段空隙、把居中弄歪。 */}
              {caption && <span className="pointer-events-none -mt-0.5 mb-px text-xs font-semibold leading-none text-white">{caption}</span>}
              <span className="leading-none">{day}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
};

export const DateRangePicker: React.FC<{
  start: string;
  end: string;
  onChange: (start: string, end: string) => void;
}> = ({ start, end, onChange }) => {
  const [open, setOpen] = React.useState(false);
  const [visibleMonth, setVisibleMonth] = React.useState(() => fromIso(start) || new Date());
  const closeRef = React.useRef<HTMLButtonElement>(null);
  const rootRef = React.useRef<HTMLDivElement>(null);
  const days = daysBetween(start, end);

  React.useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    // 点击日历外部区域关闭（桌面端 popover 无全屏遮罩，靠此关闭；移动端 fixed 遮罩
    // 在 rootRef 内，由其自身 onMouseDown 关闭，此处不重复触发）。
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('mousedown', onPointerDown);
    window.setTimeout(() => closeRef.current?.focus(), 0);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('mousedown', onPointerDown);
    };
  }, [open]);

  const select = (value: string) => {
    if (!start || end || value < start) {
      onChange(value, '');
      return;
    }
    if (daysBetween(start, value) > 14) {
      onChange(value, '');
      return;
    }
    onChange(start, value);
  };

  return (
    <div ref={rootRef} className="relative min-w-0 flex-1">
      {/**
       * 日期触发器 —— 和地点、同行同一副刻线身体。
       *
       * 标签一律在线**上方**（`FIELD_LABEL`），值一律坐在线上（15px），右端是读数。
       * **不要**把它做成填色井、把标签装进井里面：同一个角色在首屏上就会出现两种排法
       * （「从哪里出发」的标签在线上方、「精确日期」的标签在井里）。
       */}
      {/**
       * 读数**紧跟标签**，左对齐 —— 不右对齐，也不挂在值那条线的右端。
       *
       * 右端与右对齐是同一个落点：这一格的最右边，而右邻就是「同行」那一格的标签。
       * 屏幕上 `最长 14 天` 和 `同行` 只隔 20px，读起来像是同行那一格的说明。它说的是
       * **这一格**的约束（日期最多选 14 天），所以贴着这一格的标签走。
       */}
      <div className="mb-1 flex items-baseline gap-2">
        <label className={cn(FIELD_LABEL, 'mb-0')}>精确日期</label>
        {/* 空格照旧：`${days} 天 ${days - 1} 晚` 是用户看得见的文案，`p1a-trip-planner`
            按它的字面找这枚钮（`/5 天 4 晚/`）—— 所以它必须仍在按钮的可及名字里，
            见下面 `aria-label`。排版不该顺手改文案。 */}
        {/* **这一句带汉字，所以它不能用 `READOUT_LABEL`。** §Typography 写得很明确：
            `READOUT_LABEL`（等宽 + 0.12em 字距 + uppercase）是给**纯拉丁或纯数字**的，
            「把等宽加字距的排版用在中文上就是错的」—— `uppercase` 对汉字是空操作，而字距会
            落在最后一个汉字之后，把量词推出一格。

            而且它看得见：等宽栈是系统栈（嵌入字体只有拉丁 + 汉字两只），所以「最长」「天」
            这三个字形会掉到系统的 `Noto Sans CJK SC`，
            而同一行里的 `14` 由 `DejaVu Sans Mono` 渲 —— 一句七个字符用了两只都不是我们的字。

            改成 11px 的「标签 · 值」读数：sans、无字距、无 uppercase。也**不加 tabular-nums**
            —— 同一节 §Typography 禁止把它用在中西混排的值上。 */}
        <span className="shrink-0 text-[11px] font-medium text-ink-muted">
          {days > 0 ? `${days} 天 ${days - 1} 晚` : '最长 14 天'}
        </span>
      </div>
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        // 可及名字必须带上读数：`p1a-trip-planner` 按 `/2026-08-01 → 2026-08-14/` 与
        // `/5 天 4 晚/` 两种形状找这枚钮。
        aria-label={`${start || '选择开始日期'} → ${end || '选择结束日期'}${days > 0 ? ` · ${days} 天 ${days - 1} 晚` : ''}`}
        onClick={() => setOpen(true)}
        className={cn(RULED_FIELD, 'text-left')}
      >
        <CalendarDays size={17} className="shrink-0 text-chart" />
        <span className={cn('min-w-0 flex-1 truncate', FIELD_VALUE)}>
          {start || '选择开始日期'} <span className="text-chart">→</span> {end || '选择结束日期'}
        </span>
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex items-end bg-ink/25 sm:absolute sm:inset-auto sm:left-0 sm:top-[calc(100%+8px)] sm:w-[680px] sm:items-start sm:bg-transparent" onMouseDown={(event) => { if (event.currentTarget === event.target) setOpen(false); }}>
          <div role="dialog" aria-modal="true" aria-label="选择旅行日期" className="max-h-[85vh] w-full overflow-y-auto rounded-t-card border border-stroke bg-panel p-4 shadow-lg sm:rounded-card sm:p-5">
            <div className="mb-4 flex items-center justify-between">
              <button type="button" aria-label="上个月" onClick={() => setVisibleMonth((value) => addMonths(value, -1))} className="grid h-10 w-10 place-items-center rounded-card hover:bg-surface"><ChevronLeft size={17} /></button>
              <p className="text-xs text-ink-secondary">最长 14 天</p>
              <div className="flex items-center gap-1">
                <button type="button" aria-label="下个月" onClick={() => setVisibleMonth((value) => addMonths(value, 1))} className="grid h-10 w-10 place-items-center rounded-card hover:bg-surface"><ChevronRight size={17} /></button>
                <button ref={closeRef} type="button" aria-label="关闭日期选择" onClick={() => setOpen(false)} className="grid h-10 w-10 place-items-center rounded-card hover:bg-surface"><X size={17} /></button>
              </div>
            </div>
            <div className="flex gap-8">
              <Month month={visibleMonth} start={start} end={end} onSelect={select} />
              <div className="hidden flex-1 sm:block"><Month month={addMonths(visibleMonth, 1)} start={start} end={end} onSelect={select} /></div>
            </div>
            <div className="mt-4 flex items-center justify-between border-t border-stroke pt-4">
              <button type="button" onClick={() => onChange('', '')} className="min-h-10 text-sm text-ink-secondary hover:text-ink">清除日期</button>
              <button type="button" disabled={!start || !end} onClick={() => setOpen(false)} className="min-h-10 rounded-card bg-accent px-4 text-sm font-semibold text-white disabled:opacity-40">确认日期{days > 0 ? ` · ${days} 天 ${days - 1} 晚` : ''}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

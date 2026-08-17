import React from 'react';
import { useReducedMotion } from 'motion/react';
import { ArrowDown, ArrowUp, ChevronDown, MapPin, Minus, Plus, Send, Users, X } from 'lucide-react';
import { api } from '../../lib/api';
import { createPlaceSearchRunner, type PlaceSearchCandidate } from '../../lib/placeSearch';
import { describeRequestFailure, type RequestFailure } from '../../lib/requestFailureMessage';
import { RequestFailureNotice } from '../ui/RequestFailureNotice';
import { cn } from '../../lib/utils';
import type { ControlledTripIdentity, PlaceIdentity, TripPlannerConfiguration } from '../../types/api';
import { Button } from '../ui/Button';
import { Field, FIELD_LABEL, FIELD_VALUE, RULED_FIELD } from '../ui/Input';
import { Skeleton } from '../ui/Skeleton';
import { useApp } from '../../context/AppContext';
import { useSendMessage } from '../../hooks/useSendMessage';
import { DateRangePicker } from './DateRangePicker';

function factsFromNaturalText(text: string, config: TripPlannerConfiguration) {
  const origin = text.match(/从([^，,。\s]{2,12})(?:出发|走)/)?.[1] || '';
  const destination = text.match(/(?:去|前往)([^，,。\s]{2,12}?)(?:旅行|旅游|玩|待|住|，|,|。|$)/)?.[1] || '';
  const exactDates = text.match(/(20\d{2}-\d{2}-\d{2}).*?(20\d{2}-\d{2}-\d{2})/);
  const adultMatch = text.match(/(\d+)\s*(?:位|个)?成人/);
  const childMatch = text.match(/(\d+)\s*(?:位|个)?(?:儿童|孩子|小孩)/);
  const hasChild = /孩子|小孩|宝宝|亲子/.test(text);
  const elderly = /爸妈|父母|老人|长辈/.test(text);
  const fallback = config.primary_styles.find((option) => option.is_default)!;
  const inferred = config.primary_styles.find((option) => option.inference_keywords.some((keyword) => text.includes(keyword)));
  return { origin, destination, start: exactDates?.[1] || '', end: exactDates?.[2] || '', adults: Number(adultMatch?.[1] || config.default_adults), children: Number(childMatch?.[1] || (hasChild ? Math.max(1, config.default_children) : config.default_children)), elderly, primary: inferred?.label || fallback.label } as const;
}

/**
 * 芯片皮 —— 一套，规划表里所有可选项同读。
 *
 * 标签档（4px）：芯片是「a single short token」。选中态是满面暖底 + accent
 * 文字（禁止用色条表达选中）；未选中态**没有边框**，只有文字 —— 一行八个
 * 带框的方块本身就是「很多个条框」。
 */
const CHIP = 'min-h-10 rounded-label px-2.5 text-sm transition-colors duration-fast ease-standard';
const CHIP_ON = 'bg-accent/10 font-medium text-accent';
const CHIP_OFF = 'text-ink-secondary hover:bg-ink/5 hover:text-ink';

/**
 * 首屏两个入口各自的那张面。**一个入口一个框，框里的字段一个都不画框。**
 *
 * 整列就只有这两个框。**不要往里加**：字段是刻线、节与节之间靠留白、芯片是标签档的色块。
 * 一旦框开始往里长，这一列很快就会回到十几件带框/填色的元件、五种底色、四层嵌套，而
 * 卡套卡被明禁。「Depth comes from rule weight and whitespace, not radius」在这一屏
 * 仍然成立 —— 这两个框表达的是「这里有两个入口」，不是层次。
 *
 * **这个框是被它的描边定义的，不是被它的底色定义的**：paper `#F5F1E4` 与 panel `#FCFBF6`
 * 的对比度只有 **1.09:1**，「换个底色当一张面」在这套暖调纸上根本看不出来。所以
 * `border-stroke` 是必需项，`bg-panel` 只是让描边里侧不至于和纸一模一样。
 *
 * 里面那口自然语言的填色井会被 `index.css` 的 `.rounded-card .rounded-card` 自动降到标签档
 * （4px）—— 那正是那条规则存在的理由，不是它的副作用。
 */
const PANEL = 'rounded-card border border-stroke bg-panel p-5 sm:p-6';

function tripDays(start: string, end: string): number {
  if (!start || !end) return 0;
  return Math.floor((new Date(`${end}T00:00:00`).getTime() - new Date(`${start}T00:00:00`).getTime()) / 86_400_000) + 1;
}

export const PlaceField: React.FC<{
  role: 'origin' | 'destination';
  value: PlaceIdentity | null;
  onChange: (value: PlaceIdentity) => void;
  label: string;
  frequent?: boolean;
  initialQuery?: string;
  /**
    * 字段的度量由调用方给。规划表上四格并排，所以是 `flex-1`；我的偏好那一屏
   * 只有一格且只承一个城市名，通栏会印成「一条 900px 的刻线上三个字」，所以那里收到
   * `max-w-sm`。**度量是版面决定的，不是字段自己决定的**，所以它是个参数。
   */
  className?: string;
}> = ({ role, value, onChange, label, frequent, initialQuery = '', className }) => {
  const [query, setQuery] = React.useState(value?.name || initialQuery);
  const [candidates, setCandidates] = React.useState<PlaceSearchCandidate[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');

  // 多个请求会同时在飞：守卫保证只有最新那次能写 candidates/error/loading，
  // 判据与超时都在 placeSearch.ts（那里有单测）。
  const runner = React.useMemo(() => createPlaceSearchRunner(role), [role]);
  React.useEffect(() => () => runner.cancel(), [runner]);

  const search = React.useCallback(async (raw: string) => {
    const q = raw.trim();
    if (q.length < 2) return;
    await runner.run(q, { setLoading, setError, setCandidates });
  }, [runner]);

  // 只在真的选中了一个地点时同步输入框，不在没有选中时把它清空。
  // 这个 effect 挂载时也会跑一次，而挂载时 `value` 必然是 null，于是它把
  // `initialQuery`（从原始想法里抽出来的目的地）当场抹掉——补齐表单看起来预填好了，
  // 唯独丢掉用户真正说出口的那个地点。已挂载的字段不会再回到「未选中」：移除一站是
  // 整个字段卸载，选中只会写入一个地点。
  React.useEffect(() => { if (value) setQuery(value.name); }, [value]);
  // 输入即搜索（去掉搜索按钮）：随用户输入防抖自动检索，结果直接列在下方供选择。
  // 已选中的地点（query === value.name）或不足 2 字时不触发，也不残留旧候选。
  React.useEffect(() => {
    const q = query.trim();
    if (q.length < 2 || q === value?.name) {
      runner.cancel();
      setCandidates([]);
      setError('');
      setLoading(false);
      return;
    }
    const timer = window.setTimeout(() => void search(q), 280);
    return () => window.clearTimeout(timer);
  }, [query, value?.name, search, runner]);

  return (
    <div className={cn('relative min-w-0 flex-1', className)}>
      {/* 一条刻线，不是一口井（见 `ui/Input` 的说明）。地点针走图纸墨蓝：它是标记，不是控件。 */}
      <label className={FIELD_LABEL}>{label}</label>
      <div className={RULED_FIELD}>
        <MapPin size={15} className="shrink-0 text-chart" />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void search(query); } }}
          className={cn('min-w-0 flex-1 bg-transparent py-2', FIELD_VALUE)}
          placeholder={role === 'origin' ? '城市、机场或火车站' : '城市、区域或岛屿'}
          aria-label={label}
          autoComplete="off"
        />
        {frequent && value && <span className="rounded-label bg-success/10 px-1.5 py-0.5 text-[11px] text-success">常用</span>}
        {loading && <span className="text-[11px] text-ink-muted">搜索中…</span>}
      </div>
      {/* 候选浮层仍是一个**面**（overlay），所以它保留纸白底、边框与 lg 挡阴影 ——
          软阴影正是给 overlay 的。 */}
      {(candidates.length > 0 || error) && (
        <div className="absolute z-30 mt-1 max-h-64 w-full overflow-y-auto rounded-card border border-stroke bg-panel p-1 shadow-lg">
          {candidates.map(({ place }) => (
            <button
              key={place.place_id}
              type="button"
              onClick={() => { onChange(place); setCandidates([]); setError(''); }}
              className="flex min-h-11 w-full items-start gap-2 rounded-label px-3 py-2 text-left transition-colors duration-fast ease-standard hover:bg-accent-soft"
            >
              <MapPin size={14} className="mt-0.5 shrink-0 text-chart" />
              <span className="min-w-0"><strong className="block truncate text-sm font-medium text-ink">{place.name}</strong><span className="block truncate text-xs text-ink-secondary">{place.display_name}</span></span>
            </button>
          ))}
          {error && <p role="status" className="px-3 py-2 text-xs text-error">{error}</p>}
        </div>
      )}
    </div>
  );
};

export const TripPlanner: React.FC<{ guidedText?: string; compact?: boolean; initialDestinations?: PlaceIdentity[] }> = ({ guidedText = '', compact = false, initialDestinations = [] }) => {
  const { state, dispatch } = useApp();
  const { sendMessage } = useSendMessage();
  const [config, setConfig] = React.useState<TripPlannerConfiguration | null>(null);
  const [configError, setConfigError] = React.useState<RequestFailure | null>(null);
  const [origin, setOrigin] = React.useState<PlaceIdentity | null>(null);
  const extracted = React.useMemo(() => config ? factsFromNaturalText(guidedText, config) : null, [config, guidedText]);
  const [destinations, setDestinations] = React.useState<Array<PlaceIdentity | null>>(initialDestinations.length ? initialDestinations.slice(0, 3) : [null]);
  const [startDate, setStartDate] = React.useState('');
  const [endDate, setEndDate] = React.useState('');
  const [adults, setAdults] = React.useState(0);
  const [children, setChildren] = React.useState(0);
  const [advanced, setAdvanced] = React.useState(false);
  const [elderly, setElderly] = React.useState(false);
  const [accessible, setAccessible] = React.useState(false);
  const [primary, setPrimary] = React.useState('');
  const [primaryTouched, setPrimaryTouched] = React.useState(false);
  const [interests, setInterests] = React.useState<string[]>([]);
  const [more, setMore] = React.useState(false);
  const [naturalText, setNaturalText] = React.useState(guidedText);
  const [originReady, setOriginReady] = React.useState<boolean | null>(null);
  const [originSetup, setOriginSetup] = React.useState<PlaceIdentity | null>(null);
  const [error, setError] = React.useState('');
  const [promptIndex, setPromptIndex] = React.useState(0);
  const [naturalFocused, setNaturalFocused] = React.useState(false);
  /* 提示语轮播是一个 `setInterval` 换文案，不是 CSS 过渡也不是补间 —— reduced-motion 那两处
     集中定义都管不到它，所以这一支自己读设置。读法与 `BundleMapLeaflet` 同一个钩子，理由见
     那里：命令式这一层要和补间那一层认同一个来源。 */
  const reducedMotion = useReducedMotion();

  const loadConfiguration = React.useCallback(async () => {
    setConfigError(null);
    try {
      const next = await api.getTripPlannerConfiguration();
      setConfig(next);
      const facts = factsFromNaturalText(guidedText, next);
      setStartDate(facts.start); setEndDate(facts.end);
      setAdults(facts.adults); setChildren(facts.children);
      setElderly(facts.elderly || next.default_elderly_companions);
      setAccessible(next.default_accessibility_required);
      setPrimary(facts.primary);
    } catch (reason) {
      setConfigError(describeRequestFailure(reason, '读取', '旅行规划选项'));
    }
  }, [guidedText]);

  React.useEffect(() => { void loadConfiguration(); }, [loadConfiguration]);

  React.useEffect(() => {
    if (!config || naturalText || naturalFocused || reducedMotion) return;
    const timer = window.setInterval(() => setPromptIndex((index) => (index + 1) % config.inspiration_prompts.length), config.inspiration_rotation_ms);
    return () => window.clearInterval(timer);
  }, [config, naturalFocused, naturalText, reducedMotion]);

  React.useEffect(() => {
    let active = true;
    api.getDefaultOrigin().then((place) => {
      if (!active) return;
      setOrigin(extracted?.origin ? null : place);
      setOriginReady(Boolean(place));
    }).catch(() => { if (active) setOriginReady(false); });
    return () => { active = false; };
  }, [extracted?.origin]);

  const days = tripDays(startDate, endDate);
  const submitStructured = async () => {
    setError('');
    const selectedDestinations = destinations.filter((item): item is PlaceIdentity => Boolean(item));
    if (!origin || selectedDestinations.length !== destinations.length) return setError('请从地点候选中确认出发地和所有目的地');
    if (days < 1 || days > 14) return setError(days > 14 ? '单次旅行最多 14 天，请拆分后再规划' : '请选择正确的开始和结束日期');
    const identity: ControlledTripIdentity = {
      origin, destinations: selectedDestinations, start_date: startDate, end_date: endDate,
      party: { adults, children, elderly_companions: elderly, accessibility_required: accessible },
      style: { primary, secondary_interests: interests, source: primaryTouched ? 'current' : 'suggested' },
    };
    dispatch({ type: 'SET_GUIDED_INTAKE', payload: null });
    const summary = guidedText || `从${origin.name}出发，前往${selectedDestinations.map((item) => item.name).join('、')}，${startDate} 至 ${endDate}`;
    await sendMessage(summary, undefined, {
      route: 'trip_planning', controlledTripIdentity: identity, assistantPendingLabel: '正在整理旅行信息',
    });
  };

  if (configError) return <RequestFailureNotice title="暂时无法开始规划" failure={configError} onRetry={() => void loadConfiguration()} />;
  // 读取中的骨架也是**刻线形状**：它占的是「将要出现的那几条线」的位。一块 96px 高的填色
  // 方块占的是一口井的位，而这里没有井。
  if (!config || originReady === null || !extracted) {
    return (
      <div className="flex flex-col gap-6" aria-label="正在读取旅行规划配置">
        {[0, 1, 2].map((row) => (
          <div key={row} className="flex flex-col gap-2">
            <Skeleton radius="label" className="h-3 w-16" />
            <div className="h-11 border-b border-stroke" />
          </div>
        ))}
      </div>
    );
  }
  if (!originReady) {
    return (
      // 图纸上**不套卡**：这一支和下面那张规划表一样，直接坐在首页那张图纸上，边界由图纸
      // 自己的 neatline 给出（图纸档）。
      <section aria-labelledby="origin-setup-title">
        <h2 id="origin-setup-title" className="text-xl font-semibold text-ink">选择你的常用出发地</h2>
        {/* 常用出发地的唯一修改入口是「我的偏好」视图（UserPreferencesPage 的同名区块）；本产品没有「设置」视图。 */}
        <p className="mt-1 text-sm text-ink-secondary">之后可以在「我的偏好」里改。</p>
        <div className="mt-5 flex flex-col gap-4 sm:flex-row sm:items-end">
          <PlaceField role="origin" value={originSetup} onChange={setOriginSetup} label="常用出发地" />
          <Button variant="primary" className="flex-shrink-0" disabled={!originSetup} onClick={async () => {
            if (!originSetup) return;
            const saved = await api.setDefaultOrigin(originSetup);
            setOrigin(saved); setOriginReady(true);
          }}>保存并继续</Button>
        </div>
        {/* 跳过：不保存常用出发地，直接进入规划器，用户可在规划时手动填写出发地。 */}
        <button type="button" onClick={() => { setOrigin(null); setOriginReady(true); }} className="mt-4 text-[13px] font-medium text-ink-secondary underline-offset-2 transition-colors duration-fast ease-standard hover:text-ink hover:underline">
          跳过
        </button>
      </section>
    );
  }

  return (
    <div className={cn('flex flex-col gap-8', compact && 'gap-6')}>
      {/**
       * 结构化规划表 —— **字段是线**。
       *
       * 最外面一层是那个 `PANEL` 框（见文件头），**里面一件框都不加**：字段是刻线、节与节之间
       * 靠留白（`gap-8` = 32px）、芯片是标签档的色块。
       *
       * 特别不要把这一节自己做成 `rounded-card border border-stroke bg-panel p-4 shadow-sm`
        * 再往里塞填色井与带框芯片：那是一张卡坐在一张已经有 neatline、经纬网格与四道等高线的
        * 图纸上，而卡套卡被明禁 —— 唯一的可见症状是 `.rounded-card .rounded-card` 把井
        * 静默降到 4px，也就是说合同写的档位根本没生效。
       */}
      <section aria-label="结构化旅行规划器" className={PANEL}>
        <h2 className="text-xl font-semibold text-ink">规划一趟确定的旅行</h2>
        <div className="mt-5 flex flex-col gap-4 sm:flex-row sm:items-start">
          <PlaceField role="origin" value={origin} onChange={setOrigin} label="从哪里出发" frequent={!extracted.origin} initialQuery={extracted.origin} />
          {/* 出发地 → 目的地之间那一枚箭头走图纸墨蓝：它是路线符号，不是可点的东西。 */}
          <div className="hidden shrink-0 text-chart sm:flex sm:h-11 sm:items-center sm:mt-[22px]" aria-hidden>→</div>
          <div className="flex min-w-0 flex-[1.35] flex-col gap-3">
            {destinations.map((destination, index) => (
              <div key={index} className="flex items-end gap-1">
                <PlaceField role="destination" value={destination} onChange={(place) => setDestinations((items) => items.map((item, i) => i === index ? place : item))} label={index === 0 ? '去哪里' : `第 ${index + 1} 站`} initialQuery={index === 0 ? extracted.destination : ''} />
                {destinations.length > 1 && (
                  <div className="flex shrink-0 items-center gap-0.5">
                    <button type="button" aria-label="上移目的地" disabled={index === 0} onClick={() => setDestinations((items) => { const next = [...items]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; return next; })} className="grid h-11 w-8 place-items-center rounded-card text-ink-secondary transition-colors hover:bg-surface hover:text-ink disabled:opacity-30 disabled:hover:bg-transparent"><ArrowUp size={15} /></button>
                    <button type="button" aria-label="下移目的地" disabled={index === destinations.length - 1} onClick={() => setDestinations((items) => { const next = [...items]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; return next; })} className="grid h-11 w-8 place-items-center rounded-card text-ink-secondary transition-colors hover:bg-surface hover:text-ink disabled:opacity-30 disabled:hover:bg-transparent"><ArrowDown size={15} /></button>
                    <button type="button" aria-label={`删除第 ${index + 1} 个目的地`} onClick={() => setDestinations((items) => items.filter((_, i) => i !== index))} className="grid h-11 w-8 place-items-center rounded-card text-ink-secondary transition-colors hover:bg-surface hover:text-error"><X size={15} /></button>
                  </div>
                )}
              </div>
            ))}
            {destinations.length < 3 && <button type="button" className="mt-0.5 self-start text-[13px] font-medium text-accent hover:underline" onClick={() => setDestinations((items) => [...items, null])}>+ 添加目的地</button>}
          </div>
        </div>
        <div className="mt-5 flex flex-col gap-4 sm:flex-row">
          <DateRangePicker start={startDate} end={endDate} onChange={(nextStart, nextEnd) => { setStartDate(nextStart); setEndDate(nextEnd); setError(''); }} />
          {/* 同行：一条刻线上的两组 ± 读数，hover **只动墨色**。不要做成填色井、也不要给
              ± 钮铺 hover 底色：一个 32px 的方块顶着和祖父辈卡面同色的底，读起来像一块被
              挖空的地方。 */}
          <Field label="同行" className="sm:flex-none sm:w-[300px]">
            <Users size={16} className="shrink-0 text-chart" />
            {([['成人', adults, setAdults, 1], ['儿童', children, setChildren, 0]] as const).map(([label, value, setter, min]) => (
              <div key={label} className="flex items-center gap-0.5">
                <span className="mr-1 text-[13px] text-ink-secondary">{label}</span>
                <button
                  type="button"
                  aria-label={`减少${label}`}
                  disabled={value <= min}
                  onClick={() => setter(value - 1)}
                  className="grid h-8 w-8 place-items-center text-ink-secondary transition-colors duration-fast ease-standard hover:text-ink disabled:opacity-30"
                >
                  <Minus size={13} />
                </button>
                <span className="w-5 text-center font-mono text-[15px] tabular-nums">{value}</span>
                <button
                  type="button"
                  aria-label={`增加${label}`}
                  onClick={() => setter(value + 1)}
                  className="grid h-8 w-8 place-items-center text-ink-secondary transition-colors duration-fast ease-standard hover:text-ink"
                >
                  <Plus size={13} />
                </button>
              </div>
            ))}
          </Field>
        </div>
        <button type="button" data-testid="planner-party-advanced" className="mt-4 flex items-center gap-1 text-[13px] text-ink-secondary transition-colors duration-fast ease-standard hover:text-ink" onClick={() => setAdvanced(!advanced)}>其他同行需求 <ChevronDown size={13} className={cn('transition-transform duration-base ease-standard', advanced && 'rotate-180')} /></button>
        {/**
         * 其他同行需求 —— **和这一屏其余的布尔选择同一套芯片**（`CHIP` / `CHIP_ON` /
         * `CHIP_OFF`）。
         *
         * 它们的角色和 20 行之下的「这次想要」逐字相同：一次布尔偏好选择，提交时随表一起走，
         * 所以皮也必须相同。**不要**另起一套开关语汇（一枚 `Switch` 原语的 `pill` 皮和这里的
         * `CHIP_ON` 就是同一个东西 `bg-accent/10 text-accent` —— 那只是把同一个角色的两套值
         * 分住到两个文件里），也**不要**用浏览器原生 `<input type="checkbox">`：方角、13px、
         * 系统蓝勾，是这一屏上唯一完全落在设计系统之外的控件。注意这两项藏在一道折叠后面，
         * 整屏改样式时容易漏掉它们。
         *
         * `aria-pressed` 与两枚芯片同写：它是这一行**唯一**的选中态载体（芯片没有原生
         * checkbox 自带的那一个），判据按 `data-testid` 找钮、按 `aria-pressed` 读态。
         */}
        {advanced && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {([['elderly', elderly, setElderly, '老人同行'], ['accessible', accessible, setAccessible, '需要无障碍']] as const).map(([id, checked, setter, label]) => (
              <button
                key={id}
                type="button"
                data-testid={`planner-party-${id}`}
                aria-pressed={checked}
                onClick={() => setter(!checked)}
                className={cn(CHIP, checked ? CHIP_ON : CHIP_OFF)}
              >
                {label}
              </button>
            ))}
          </div>
        )}

        {/**
         * 这次想要 —— **一套芯片皮，全表只有一套**。
         *
         * 芯片一律标签档（4px），选中 = `bg-accent/10 text-accent`，未选中 = 只有文字。
         * 「更多偏好」和「+ 添加目的地」是同一类动作（展开更多），所以是同一种文字钮。
         *
         * 三件事都不许做：卡档带框的芯片（`min-h-10 rounded-card border`）；胶囊芯片
         * （`rounded-full` ——「A container is never a circle」明禁一个装着文字的
         * 圆容器）；给选中态再印一枚 `Check` 字形 —— 「被选中」已经由满面暖底说过了。
         */}
        <div className="mt-6">
          <p className={FIELD_LABEL}>这次想要</p>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            {config.primary_styles.map((style) => (
              <button
                key={style.id}
                type="button"
                aria-pressed={primary === style.label}
                onClick={() => { setPrimary(style.label); setPrimaryTouched(true); }}
                className={cn(CHIP, primary === style.label ? CHIP_ON : CHIP_OFF)}
              >
                {style.label}{style.is_default && !primaryTouched && ' · 建议'}
              </button>
            ))}
            <button type="button" className="ml-1 text-[13px] font-medium text-accent transition-colors duration-fast ease-standard hover:underline" onClick={() => setMore(!more)}>更多偏好</button>
          </div>
          {more && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {config.secondary_interests.map((interest) => (
                <button
                  key={interest.id}
                  type="button"
                  aria-pressed={interests.includes(interest.label)}
                  disabled={!interests.includes(interest.label) && interests.length >= config.max_secondary_interests}
                  onClick={() => setInterests((items) => items.includes(interest.label) ? items.filter((item) => item !== interest.label) : [...items, interest.label])}
                  className={cn(CHIP, interests.includes(interest.label) ? CHIP_ON : CHIP_OFF, 'disabled:opacity-40')}
                >
                  {interest.label}
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="mt-7 flex flex-col items-start justify-between gap-3 sm:flex-row sm:items-center"><p className="text-sm text-error" role="status">{error}</p><Button variant="primary" onClick={() => void submitStructured()} disabled={state.isStreaming}>开始规划</Button></div>
      </section>

      {/**
       * 自然语言入口 —— **首屏唯一保留填色的字段**。
       *
       * 「一条线承一个值，一口井承一段话」（见 `ui/Input`）：这里要写的是句子，那口暖底本身
       * 就在说这件事。
       *
       * 分节由**面板自己的描边**承担，**不另加 `border-t`**：一条分节线 + 一个框是两处表达
       * 同一次分节。标题是 20px —— 它和「规划一趟确定的旅行」是**同一个角色**（首屏两个入口
       * 各自的标题），两处必须同一挡。
       */}
      {!compact && (
        <section className={PANEL} aria-label="自然语言旅行输入">
          <h2 className="text-xl font-semibold text-ink">还没完全想好？</h2>
          <div className="mt-3 flex items-end gap-2 rounded-card border border-stroke bg-surface p-2 transition-colors duration-base ease-standard focus-within:border-accent">
            <textarea
              aria-label="描述旅行想法"
              value={naturalText}
              onFocus={() => setNaturalFocused(true)}
              onBlur={() => setNaturalFocused(false)}
              onChange={(event) => setNaturalText(event.target.value)}
              rows={2}
              className="min-h-14 flex-1 resize-none bg-transparent px-2 py-1 text-[15px] text-ink placeholder:text-ink-muted"
              placeholder={`例如：${config.inspiration_prompts[promptIndex]}`}
            />
            <Button aria-label="发送旅行想法" variant="primary" disabled={!naturalText.trim() || state.isStreaming} onClick={() => void sendMessage(naturalText, undefined, { assistantPendingLabel: '正在判断任务' })}><Send size={15} /></Button>
          </div>
        </section>
      )}
    </div>
  );
};

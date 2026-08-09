import { BedDouble, Landmark, Route, Utensils, type LucideIcon } from 'lucide-react';
import type { SelectionSlotType } from '../types/delivery';

/**
 * 行程的四个域，在全部交付面上是同一套身份。
 *
 * 域身份按 `entity_kind` 取，**不按 `role`**：role 只有「地点 / 移动 / 抵达 / 离开」四挡，
 * 而**景点与餐饮同属「地点」** —— 浅草寺和大黑家天麸罗在纸上会是逐像素相同的一枚蓝点加
 * 一个「地点安排」，四个域看起来是一个域，整天的行程无法扫读。
 * 选项卡与时间轴共用这一张表：一个域的字形、名词、印记只在这里写一次。
 *
 * `mark` 是这个域在纸上的**记号**，四个必须能在余光里分开，而且只用已有色票：
 *
 * - 景点 `vermilion` 实心 —— 「朱红是标记不是控件：品牌点、**航点图钉**、
 *   邮戳」。一个景点就是一枚航点图钉，这是色票里唯一为「地点标记」保留的声部。
 * - 交通 `chart` 实心 —— chart 是印刷声部，路线与航路本来就归它。
 * - 用餐 `ink` 实心 —— 中性墨色，不占任何语义声部。
 * - 住宿 `panel` 空心 + 墨色描边 —— 第四个记号靠**轮廓**而不是第四种颜色区分，
 *   因为色票里已经没有能用的颜色了：accent 是唯一的交互蓝（记号不可交互），
 *   amber 归风险，green 归已核实，red 归破坏性。硬造一个域色会同时破坏这四条。
 */
export type TripDomain = SelectionSlotType;

export interface TripDomainPresentation {
  /** 域字形。交通有具体方式时由调用方换成方式字形（高铁/航班/步行…）。 */
  icon: LucideIcon;
  /** 域名词，时间轴的类型标签与选项卡共用。 */
  noun: string;
  /** 选项卡标题。 */
  heading: string;
  /** 选项卡里一个方案的名字。 */
  plan: string;
  /** 时间轴记号的圆点类名（含 4px 光环）。 */
  dot: string;
  /** 记号里字形的类名——实心记号用纸色字形，空心记号用墨色字形。 */
  glyph: string;
}

export const TRIP_DOMAIN_PRESENTATION: Record<TripDomain, TripDomainPresentation> = {
  visit: {
    icon: Landmark,
    noun: '景点',
    heading: '景点选择',
    plan: '景点方案',
    dot: 'bg-vermilion ring-vermilion/15',
    glyph: 'text-panel',
  },
  transport: {
    icon: Route,
    noun: '交通',
    heading: '交通选择',
    plan: '交通方案',
    dot: 'bg-chart ring-chart/15',
    glyph: 'text-panel',
  },
  dining: {
    icon: Utensils,
    noun: '用餐',
    heading: '用餐选择',
    plan: '用餐方案',
    dot: 'bg-ink ring-ink/15',
    glyph: 'text-panel',
  },
  lodging: {
    icon: BedDouble,
    noun: '住宿',
    heading: '住宿选择',
    plan: '住宿方案',
    /* 光环一律 `/15`，四个域同一个值 —— `/12` 和 `/15` 在屏幕上是同一个灰，多一个值只是
       多一处能漂的地方。空心记号与实心记号的区别由 `bg` 与那条 1.5px 墨边表达，不借光环
       浓度再说第二遍。 */
    dot: 'bg-panel ring-ink/15 border-[1.5px] border-ink',
    glyph: 'text-ink',
  },
};

/**
 * 一条事实 / 一条亮点 `标签：值` 拆成两级。
 *
 * 分隔符是**全角**冒号 `：`，投影层两侧都这么写：`delivery_presentation._row` 是
 * `f"{label}：{value}"`，`trip_highlights` 的四条也是 `f"{label}：{body}"`。按半角
 * `:` 找会一条都拆不开——而且拆不开时整条串会被当成「值」渲染，看起来像成功了。
 *
 * 拆不开的原样当值返回：这里不假设形状，拆不开就不拆。
 */
const FULL_WIDTH_COLON = '：';

export function splitLabelledFact(fact: string): { label: string | null; value: string } {
  const at = fact.indexOf(FULL_WIDTH_COLON);
  if (at <= 0 || at === fact.length - FULL_WIDTH_COLON.length) {
    return { label: null, value: fact };
  }
  return {
    label: fact.slice(0, at).trim(),
    value: fact.slice(at + FULL_WIDTH_COLON.length).trim(),
  };
}

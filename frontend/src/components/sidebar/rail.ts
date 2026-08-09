/**
 * 轨道的刻度栏 —— 侧栏两态共用的一条几何。
 *
 * 几何写在这里、两态同读。**每处各算一遍，就会各偏一点** —— 而偏差全都来自同一个形状：
 * 一个「只淡出、不让位」的标签。`opacity-0` **不退出布局**，所以折叠态里 40px 可用宽装着
 * `Plus(18px)` + `ml-1` + 一个被压到 18px 的标签（`overflow-hidden` 让它的最小尺寸变成 0）
 * 时，内容正好 40px、`justify-center` 一点作用也没有，加号被顶在最左边、中心落在距轨道边
 * 23px 处，比轨道中心线（34px）偏左 11px。同一个形状在头部是：品牌块被压到 0 宽却留着
 * `ml-1`，把折叠开关往右推 4px。
 *
 * 所以：
 *
 * ```
 * 折叠 68px:            展开 280px:
 * │ 14 │  40  │ 14 │    │ 14 │  40  │ 标签从 54px 起 ………………… │
 *      └ 中心 34 ┘            └ 中心 34 ┘
 * ```
 *
 * 一条槽，40px 见方，左缘距轨道边 14px。折叠态 14 + 40 + 14 = 68，**槽正好居中**；
 * 展开态同一条槽变成左脊，标签一律从 54px 起排。于是折叠/展开之间**每一枚字形的横坐标都
 * 不变**，动的只有轨道自己的边（那是 §Motion 唯一获准的布局过渡）与标签的不透明度。
 *
 * 标签一律**绝对定位，不占流**。这既是上面那个偏移的结构性解（占不到流就推不动任何东西），
 * 也让 §Motion「rail 里面只动 opacity」真正成立：几何是静态的，连 snap 都没有，轨道自己的
 * `overflow-hidden` 负责把标签裁掉。
 */

/** 组容器的左右留白：8px。加上行内缩进 6px 得到槽的左缘 14px。 */
export const RAIL_GUTTER = 'px-2';

/** 行内缩进：6px。`RAIL_GUTTER` + 这一档 = 14px。 */
export const RAIL_SLOT_INDENT = 'pl-1.5';

/** 字形槽：40px 见方，内容居中。折叠态它的中心就是轨道中心线。 */
export const RAIL_SLOT = 'grid h-10 w-10 shrink-0 place-items-center';

/**
 * 行标签：从 54px 起，绝对定位不占流，只动 opacity。
 * 折叠时不必自己收宽度 —— 轨道 `overflow-hidden` 会裁。
 */
export const RAIL_LABEL =
  'pointer-events-none absolute left-[54px] whitespace-nowrap transition-opacity duration-slow ease-standard';

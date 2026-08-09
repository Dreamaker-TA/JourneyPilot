/**
 * 「第 N 天」这一枚牌 —— 一天在**所有交付面上**的同一个身份。
 *
 * 总览卡、日头、地图路线**都读这一份**。日号在几个面上各写一份实现的话，同一个产品里
 * 「第 3 天」就会有好几种颜色（交互蓝 / `--chart-day-3` / 路线色）—— 而总览恰好是打开行程后
 * 第一眼看到的那一面。
 *
 * 日号**不能用 `text-accent`**：「`accent` is the only interactive blue」，而日号不可点
 * （整张卡才是按钮）；同一张卡上「查看详情」也是 accent，一个标签和一个真正的入口于是
 * 同色同权。
 *
 * 颜色只上描边与 8% 底、字始终是 ink —— 十个分类色里有 `#FFD60A` 有 `#00C7BE`，着色到字上
 * 第 9 天就读不出来了。
 */

/** 这一天在**地图上**是什么颜色。序号口径与 `BundleMapLeaflet` 逐字相同。 */
export function dayAccent(day: number): string {
  const index = Math.max(0, day - 1) % 10;
  return `var(--chart-day-${index + 1})`;
}

/* 没有 tabular-nums：`第 1 天` 是中英混排，而等宽数字在 CJK 字体下会把全角字符也撑成
   数字宽；单个日号也没有需要对齐的列。 */
export function DayOrdinalChip({ day }: { day: number }) {
  const accent = dayAccent(day);
  return (
    <p
      data-day-chip
      // `inline-flex` 而不是 `flex`：这枚牌要按文字宽度收边。日头里它在一个 flex 行内，
      // `flex` 看不出差别；总览卡里它的父级是普通块，`flex` 会把它拉满整列。
      className="inline-flex shrink-0 items-center rounded-label border-[1.5px] px-2 py-[3px] font-mono text-[12px] font-semibold tracking-[0.06em] text-ink"
      style={{
        borderColor: accent,
        backgroundColor: `color-mix(in srgb, ${accent} 8%, transparent)`,
      }}
    >
      第 {day} 天
    </p>
  );
}

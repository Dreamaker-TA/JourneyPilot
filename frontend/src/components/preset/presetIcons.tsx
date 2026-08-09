import React from 'react';

/**
 * 旅行风格的分类字形 —— 13 枚测绘记号。
 *
 * **这是唯一一张表**，选择器的选项由它导出（`PRESET_ICON_CHOICES`）。别处再写一份 13 项的
 * 列表，两张表就会不一致，而 `?? Compass` 那类兜底会把一个缺失渲染成另一个意思。
 *
 * **为什么是手写 SVG 而不是位图。** 产品在 14px（`PresetSelector`）与 18px（`PresetCard`、
 * 选择器）两档渲染它们，位图细线在这两档发灰糊掉，而仓里没有矢量化工具（potrace /
 * inkscape / imagemagick / cairosvg 都没有）。矢量在任何一档都是清的，整套 13 枚加起来还比
 * 一张 PNG 小。**这与 `ui/ChartMark.tsx` 用位图遮罩不矛盾**——那三张是 104–112px 的单一
 * 尺寸档，这十三枚要在 14 与 18 两档都成立。**尺寸档决定载体。**
 *
 * **词汇**：用制图笔和直尺画的测绘记号，不是应用图标。所以是 butt 端头、miter 尖角、
 * 全程等粗、绝无圆角 —— 与 §Shape 图纸层「半径 0，边界由 neatline 给出」同一支词汇。
 */

/** 24 格画布上的统一笔画。1.75 在 14px 上落到约 1.02px，在 18px 上约 1.31px，两档都是实线。 */
const STROKE = 1.75;

/**
 * 每一枚的几何。写成数据而不是 13 个组件，是为了让「有几枚」「叫什么」只有一处答案
 * —— 选择器、卡片、判据都从这里数。
 */
const GLYPHS: Record<string, { label: string; draw: React.ReactNode }> = {
  // 双同心圆 + 四条外径刻线：罗盘玫瑰缩到只剩刻线，与空态那枚航图碎片同一个出处。
  // 刻线取 4 条而不是 8 条 —— 8 条在 14px 上并成一圈灰。
  compass: {
    label: '罗盘',
    draw: (
      <>
        <circle cx="12" cy="12" r="7" />
        <circle cx="12" cy="12" r="3" />
        <path d="M12 1.5V4M12 20V22.5M1.5 12H4M20 12H22.5" />
      </>
    ),
  },
  // 柱廊：一条顶横 + 三根柱子。顶横比柱子宽，所以silhouette 与「三枚并排矩形」不会混。
  landmark: {
    label: '地标',
    draw: (
      <>
        <path d="M3 7h18" />
        <path d="M7 9.5V19M12 9.5V19M17 9.5V19" />
      </>
    ),
  },
  // 盘 + 左右两件餐具。三个元素，14px 上仍然读得出「中间一个圆，两边各一竖」。
  utensils: {
    label: '餐具',
    draw: (
      <>
        <circle cx="12" cy="12.5" r="4.75" />
        <path d="M3.5 4.5V20.5M20.5 4.5V20.5" />
      </>
    ),
  },
  // 大小两枚圆共一条基线：一大一小并肩站着。
  // 出图那版画的是**三**枚递减圆，2K 上很好，18px 上三枚互切的圆并成一团 —— 圆之间要留
  // 至少 1.5 格才看得出是几个。两枚同时也更贴「亲子」这两个字。
  baby: {
    label: '亲子',
    draw: (
      <>
        <path d="M2 19.5h20" />
        <circle cx="8" cy="15" r="4.5" />
        <circle cx="17" cy="16.75" r="2.75" />
      </>
    ),
  },
  // 硬币：一个圆加一条水平弦。弦在圆心之下，所以切出的是一枚硬币的厚度而不是一条直径。
  'piggy-bank': {
    label: '储蓄',
    draw: (
      <>
        <circle cx="12" cy="12" r="8" />
        <path d="M4.25 14h15.5" />
      </>
    ),
  },
  // 菱形内套正方，正方的四角正好落在菱形的四条边上（|x-12|+|y-12| = 9）：一枚宝石。
  crown: {
    label: '皇冠',
    draw: (
      <>
        <path d="M12 3L21 12L12 21L3 12Z" />
        <path d="M7.5 7.5h9v9h-9Z" />
      </>
    ),
  },
  // 两枚不等高三角共基线，右边那枚的左坡穿过左边那枚的右坡 —— 山不是并排摆的。
  mountain: {
    label: '山峰',
    draw: (
      <>
        <path d="M2 20L9 6L16 20Z" />
        <path d="M13.5 20L18 11.5L22.5 20Z" />
      </>
    ),
  },
  // 方内套同心圆：一枚镜头。外形是方，所以与罗盘（外形是圆）在 14px 上也分得开。
  camera: {
    label: '相机',
    draw: (
      <>
        <path d="M3.5 3.5h17v17h-17Z" />
        <circle cx="12" cy="12" r="4.5" />
      </>
    ),
  },
  // 两枚等大交叠圆。不画心形轮廓 —— 心形有圆角，那是这套词汇里没有的东西。
  heart: {
    label: '爱心',
    draw: (
      <>
        <circle cx="8.5" cy="12" r="6" />
        <circle cx="15.5" cy="12" r="6" />
      </>
    ),
  },
  // 三枚并排等宽矩形：一条色标。没有顶横，所以与柱廊分得开。
  palette: {
    label: '艺术',
    draw: (
      <>
        <path d="M3 5h5v14h-5Z" />
        <path d="M9.5 5h5v14h-5Z" />
        <path d="M16 5h5v14h-5Z" />
      </>
    ),
  },
  // 符头 + 竖干 + 顶端短旗。竖干正好切在符头的右缘上。
  music: {
    label: '音乐',
    draw: (
      <>
        <circle cx="8.75" cy="17.25" r="3.25" />
        <path d="M12 17.25V5h6" />
      </>
    ),
  },
  // 经纬网：一张图幅被一横一竖分成四格。
  // 第一版画的是「矩形 + 一条折线」（图上的一条路线），18px 上那条折线的尖顶让整枚读成
  // 一张**照片占位图**，而且与旁边的山峰撞形。经纬网是图幅本身的记号，不会被读成别的东西。
  map: {
    label: '地图',
    draw: (
      <>
        <path d="M3 5h18v14H3Z" />
        <path d="M12 5v14M3 12h18" />
      </>
    ),
  },
  // 正三角 + 倒三角交叠成六角星。全是直线尖角，与那枚圆角五角星不是一套词汇。
  star: {
    label: '星标',
    draw: (
      <>
        <path d="M12 3L19.79 16.5H4.21Z" />
        <path d="M12 21L4.21 7.5H19.79Z" />
      </>
    ),
  },
};

/** 选择器的选项就是这张表本身，顺序固定。**加一枚字形就等于多一个选项，不会再有第二张表。** */
export const PRESET_ICON_CHOICES: ReadonlyArray<{ value: string; label: string }> = Object.entries(
  GLYPHS,
).map(([value, { label }]) => ({ value, label }));

/**
 * 认不出的名字画什么：**什么都不画。**
 *
 * **不要给它兜底一枚别的字形**（`?? Compass` 那类）：那是把一个缺失渲染成另一个意思。
 * 一个字形位如果没有字形，它就该是空的 —— 空位看得见、看得懂，也不会撒谎。
 */
export function getPresetIcon(name: string, size: number): React.ReactNode {
  const glyph = GLYPHS[name];
  if (!glyph) return null;
  return (
    <svg
      data-style-glyph={name}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={STROKE}
      strokeLinecap="butt"
      strokeLinejoin="miter"
      aria-hidden
      focusable="false"
    >
      {glyph.draw}
    </svg>
  );
}

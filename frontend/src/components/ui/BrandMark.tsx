import React from 'react';
import { cn } from '../../lib/utils';
import brandIcon from '../../assets/brand/brand-icon-ink.webp';
import brandWord from '../../assets/brand/brand-word-ink.webp';

/**
 * JourneyPilot 的品牌标记 —— 纸飞机 + 衬线字标。
 *
 * **素材来源是唯一的，以落地页为准**：`LagomWEB/public/projects/journeypilot/assets/` 下的
 * `brand-icon-ink.webp` / `brand-word-ink.webp`。两个仓印同一枚标 —— **不要**在 App 里另画
 * 一份，那会让同一个产品对外有两枚不同的主标记。
 *
 * 取 `-ink` 那一对而不是归档母版那对棕金的：App 的纸面是 `#f5f1e4`，棕金字标（`#8A6844`
 * 一带）在 20px 下几乎看不见，藏青这对在 20 / 26px 都读得出。`-ink` 正是落地页给它自己的
 * **奶油色面**（登机牌）准备的那一版，同一个用途。
 *
 * 位图：这两个文件就是落地页的成品资产，仓内也没有矢量化工具。只在 20–26px 一个档用；
 * 要更大的尺寸得回 LagomWEB 的 800px 母版重出一版。
 *
 * 单一亮色主题（`index.css` 里没有 `prefers-color-scheme` / `data-theme` / `.dark`），
 * 所以不需要备一份浅色字标；哪天真上深色主题，得把落地页的非 `-ink` 那一对一起带过来。
 */

/** 纸飞机。取证钩子 `data-brand-mark`：判据要能定位到标记本身，而不是数某一块里有几个图。 */
export const BrandMark: React.FC<{ size?: number; className?: string }> = ({ size = 24, className }) => (
  <img
    data-brand-mark
    src={brandIcon}
    alt=""
    aria-hidden
    // 原始 116×96，非正方。按高定尺寸、宽自适应，避免任何一处把它压变形。
    height={size}
    style={{ height: size, width: 'auto' }}
    className={cn('block select-none', className)}
    decoding="async"
  />
);

/**
 * 「JOURNEYPILOT」字标。
 *
 * 产品名用**字标本身**，不是一句系统无衬线（`<span class="text-sm font-semibold">` 那种）——
 * 品牌有一支字标，就不该让界面字体来排产品名。它是图，所以给 `role="img"` + `aria-label`，
 * 读屏仍然读得到产品名。
 */
export const BrandWordmark: React.FC<{ height?: number; className?: string }> = ({ height = 13, className }) => (
  <img
    data-brand-wordmark
    src={brandWord}
    alt="JourneyPilot"
    height={height}
    style={{ height, width: 'auto' }}
    className={cn('block select-none', className)}
    decoding="async"
  />
);

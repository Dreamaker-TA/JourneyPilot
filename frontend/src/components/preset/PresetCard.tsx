import React from 'react';
import { m } from 'motion/react';
import { cn } from '../../lib/utils';
import { staggerItem } from '../../lib/motion';
import { READOUT_LABEL } from '../../lib/typography';
import type { TravelPreset, PresetCategory } from '../../types/preset';
import { PRESET_CATEGORY_LABELS } from '../../types/preset';
import { getPresetIcon } from './presetIcons';
import { ConfirmAction } from '../ui/ConfirmAction';
import {
  Zap,
  Play,
  Pencil,
  Trash2,
} from 'lucide-react';

interface PresetCardProps {
  preset: TravelPreset;
  isActive?: boolean;
  onActivate?: (preset: TravelPreset) => void;
  onEdit?: (preset: TravelPreset) => void;
  onDelete?: (preset: TravelPreset) => void;
}

/**
 * 一张旅行风格卡。
 *
 * 三条规矩：
 *
 * 1. **分类不是一枚彩色药丸。** 分类与来源走**读数声部**（等宽 + 字距 + `ink-muted`，与
 *    时间线的类型标签同一份词汇），颜色只留给「已激活」那一个真状态。**不要**给十个分类
 *    各配一个 Tailwind 原生色：套件的颜色表只有十二个命名色，pink / lime / rose / cyan /
 *    orange 一个都不在里面，而 amber 与 green 在 §Color 里各有专门语义 ——「Warm amber is
 *    for risk and attention, **not decoration**」「Green is for verified, remembered, or
 *    satisfied constraints」。也不要兜底 `gray-100 / gray-600`：硬编码中性色不跟着主题翻。
 *
 * 2. **关键词全印，不截断。** 官方风格每个恰好四个关键词，`slice(0, 3)` + `+N` 会让每一张
 *    卡都印着「+1」，而那一行右边还空着两百多像素 —— 那个「+1」是代码里的 3 造成的，不是
 *    版面造成的。
 *
 * 3. **横向的空要用掉。** 描述与关键词**通栏**，不缩在字形右边那一列。网格在 xl 上排三列
 *    （九个官方风格正好三行三列，最后一行不剩孤卡）。不加 `line-clamp-2`：描述在这个宽度下
 *    只占一行，而真有人写长了，折行是诚实的、截断不是。
 */
export const PresetCard: React.FC<PresetCardProps> = ({
  preset,
  isActive,
  onActivate,
  onEdit,
  onDelete,
}) => {
  const icon = getPresetIcon(preset.icon, 18);
  const categoryLabel = PRESET_CATEGORY_LABELS[preset.category as PresetCategory] || preset.category;
  const focusAreas = preset.constraints.focus_areas ?? [];
  // 分类回答「这是哪一类风格」，来源回答「谁写的」。两者是同一种信息，排成一行读数。
  const kindReadout = [categoryLabel, preset.is_preset ? '官方' : null].filter(Boolean).join(' · ');

  return (
    // 入场编排挂在**卡自己**身上，不是外面再包一层：`preset-library.spec.ts` 量的是
    // 「卡的直接父级是一张三轨网格」，多一层包装（哪怕 `display: contents`）都会让
    // `..` 解析到那一层、`gridTemplateColumns` 读成 `none`。变体经 motion 的 context
    // 从网格那一层的 stagger 容器传下来，所以挂在这里就够。
    <m.div
      variants={staggerItem}
      data-testid={`preset-card-${preset.id}`}
      data-preset-active={isActive ? 'on' : 'off'}
      className={cn(
        // hover 是高频回声，走 fast 那一挡（token 表：`duration.fast` = 「hover/focus echoes」）。
        //
        // 悬停是**一层暖底 + 边框着色**，和四个侧屏的刻线行同一副语法。**不抬升、不加
        // 阴影**：校准表在「克制底线 / warm·claude」那一行明写
        // 「hover 不 scale/lift」，而 §Component Rules 写着「Very soft shadow **only on
        // major surfaces or overlays**」——一张网格里的卡两者都不是。抬升还有一个更实的
        // 代价：一个会浮起来的方块读起来像可以拖走，而它不能。
        //
        // 已激活态也是边框 + 满面暖底，**不压** `shadow-[0_0_0_2px_…]` 那类环：它是表外
        // 阴影，而 §Anti-Slop 又禁止用色条表达选中。
        'flex flex-col gap-2.5 rounded-card border bg-panel p-4 transition-colors duration-fast',
        isActive
          ? 'border-accent bg-accent/[0.05]'
          // 边用 `stroke` **整值**，不是 `stroke/20`：§Component Rules 写的是「1px warm
          // border」，而 20% 的暖灰在纸白上基本看不见，卡片就只剩阴影在浮着。
          : 'border-stroke hover:border-accent/40 hover:bg-accent/[0.02]'
      )}
    >
      <div className="flex items-start gap-2.5">
        {/* 字形槽**不填色**：一张卡里再放一个 36px 的实色方块就是盒中盒，而这枚测绘记号
            本来就是画在纸上的线条，衬底反而把它闷住。着色只区分「这一张是不是激活中」。 */}
        <div
          className={cn(
            'flex size-9 flex-shrink-0 items-center justify-center',
            isActive ? 'text-accent' : 'text-ink-secondary'
          )}
        >
          {icon}
        </div>

        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold text-ink">{preset.name}</h3>
          <p
            data-testid={`preset-card-kind-${preset.id}`}
            className={cn('mt-0.5 text-ink-muted', READOUT_LABEL)}
          >
            {kindReadout}
          </p>
        </div>
      </div>

      <p className="break-words text-xs leading-relaxed text-ink-secondary">
        {preset.description}
      </p>

      {/* 关键词与使用次数是这张卡的**内容**，不是读数标签，所以走类型表最小的那一挡
          12px（§Typography：「12px: badges, metadata」）。**不要压到 10px**：表里没有这一挡，
          而这些串全是中文，10px 的汉字在 4KB 子集之外的系统字体下已经糊了。 */}
      {focusAreas.length > 0 && (
        <div data-testid={`preset-card-focus-${preset.id}`} className="flex flex-wrap gap-1">
          {focusAreas.map((area) => (
            <span
              key={area}
              className="rounded-label bg-ink/5 px-1.5 py-0.5 text-xs text-ink-secondary"
            >
              {area}
            </span>
          ))}
        </div>
      )}

      <div className="mt-auto flex items-center justify-between gap-2 border-t border-stroke/60 pt-2.5">
        <span className="text-xs text-ink-muted">
          {preset.usage_count > 0 ? `使用 ${preset.usage_count} 次` : '尚未使用'}
        </span>

        <div className="flex items-center gap-1">
          {!preset.is_preset && onEdit && (
            <button
              type="button"
              aria-label="编辑这个风格"
              onClick={(e) => { e.stopPropagation(); onEdit(preset); }}
              className="rounded-card p-1.5 text-ink-muted transition-colors duration-fast hover:bg-ink/5 hover:text-ink"
            >
              <Pencil size={13} />
            </button>
          )}
          {!preset.is_preset && onDelete && (
            <div onClick={(e) => e.stopPropagation()}>
              <ConfirmAction
                onConfirm={() => onDelete(preset)}
                confirmLabel="删除"
                className="border-transparent bg-transparent px-0 py-0"
              >
                <Trash2 size={13} />
              </ConfirmAction>
            </div>
          )}
          {onActivate && (
            <button
              onClick={(e) => { e.stopPropagation(); onActivate(preset); }}
              className={cn(
                'flex items-center gap-1 rounded-label px-2 py-1 text-xs font-medium transition-colors duration-fast',
                // 未激活是**文字动作**，不是一个填色块：九张卡上九个蓝方块，等于一屏
                // 九个 accent 时刻（克制底线是一屏一个）。
                // 激活中的那一张保留实色 —— 它是这一屏唯一真正的状态。
                isActive
                  ? 'bg-accent text-white'
                  : 'text-accent hover:bg-accent/10'
              )}
            >
              {isActive ? (
                <>
                  <Zap size={11} />
                  已激活
                </>
              ) : (
                <>
                  <Play size={11} />
                  激活
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </m.div>
  );
};

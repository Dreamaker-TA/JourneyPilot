import React from 'react';
import { m } from 'motion/react';
import { cn } from '../../lib/utils';
import { staggerContainer, staggerItem } from '../../lib/motion';

/**
 * 发丝刻线行 —— 侧屏列表的那一种形态。
 *
 * 一条记录是**一条行**，不是一张卡：满列宽、左边主词、右边右对齐的等宽读数、行与行之间
 * 一条发丝线。零边框、零圆角、零阴影，悬停只上一层暖底（不抬升、不加框 —— 抬升会让一行
 * 看起来能被拖走）。
 *
 * 为什么不能画成卡（`rounded-card border border-stroke bg-panel p-4 shadow-sm`）：
 * 「Very soft shadow **only on major surfaces or overlays**」——一条列表行两者都不是；
 * 「Use cards for concrete objects only」——列表里有「一组偏好选项」「一个资料来源的段数」
 * 这种非对象。空间上也不划算：一行真正用掉的横向宽度只有约 20%，卡的右边会空掉约 800px。
 * 「Depth comes from rule weight and whitespace, not radius」。
 */

/**
 * 行容器。`count` 传进来是给 stagger 封顶用的：40ms/项在 12 项时正好撞上
 * `stagger.maxTotal` 的 480ms 上限，不传就等于把上限交给运气。
 */
export const RuledList: React.FC<{
  count: number;
  /** 列表顶部也画一条线 —— 上面紧接着标题栏时留白已经足够，通常不画。 */
  topRule?: boolean;
  className?: string;
  children: React.ReactNode;
}> = ({ count, topRule = false, className, children }) => (
  <m.div
    variants={staggerContainer(count)}
    initial="hidden"
    animate="visible"
    // 末尾那条线要画：`divide-y` 只画**行与行之间**，于是最后一行下面是敞口的，一张
    // 表读起来像没写完。收口的线让列表有下边界（而这一条同时替掉了各屏各自在页脚
    // 上面再补一条 `border-t` 的做法 —— 那样会在 24px 内出现两条平行线）。
    className={cn('divide-y divide-stroke/60 border-b border-stroke/60', topRule && 'border-t border-stroke/60', className)}
  >
    {children}
  </m.div>
);

/**
 * 一条行。
 *
 * `onClick` 在的时候它是一枚 `<button>`（可及名字 = 行里的全部文字），不在的时候是
 * 一段 `<div>`。**一条能点的行就是入口本身** —— 不要退回「先选中、再去别处按一枚按钮」
 * 那种两步。
 */
export const RuledRow: React.FC<{
  /** 主词。15px/600 —— 「reading」那一挡，列表主词就住在这里。 */
  title: React.ReactNode;
  /** 主词下面那一行元信息。11px 读数或 12px 短句，由调用方给。 */
  meta?: React.ReactNode;
  /** 右侧：读数、状态标、动作簇。右对齐。 */
  trailing?: React.ReactNode;
  onClick?: () => void;
  /** 当前正开着的那一条。满面暖底 + 主词着色，不画任何边（no colour bar）。 */
  active?: boolean;
  testId?: string;
  className?: string;
  /**
   * 可及名字用的那一个**规范字符串**。
   *
   * 主词被拆成字段来排版（出发地 / 箭头 / 目的地各有声部），而拆分是给眼睛的：
   * 读屏与判据应该拿到那一条完整标题，不是三段被拼起来的碎片。侧栏那条记录行
   * 出于同一个理由也这么做。
   */
  label?: string;
}> = ({ title, meta, trailing, onClick, active, testId, className, label }) => {
  const body = (
    <>
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span
          className={cn(
            'min-w-0 break-words text-[15px] font-semibold leading-snug',
            active ? 'text-accent' : 'text-ink'
          )}
        >
          {title}
        </span>
        {meta && (
          <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-secondary">
            {meta}
          </span>
        )}
      </span>
      {trailing && (
        <span className="flex flex-shrink-0 items-center gap-2 self-start pt-0.5">{trailing}</span>
      )}
    </>
  );

  // 行内左右内边距是 `px-2` 而不是 0：悬停的底要比文字宽出一点才像「指着这一行」，
  // 而发丝线由容器的 `divide-y` 画在行的边界上，不受这一点内边距影响。
  const shape = cn(
    'flex w-full items-start gap-4 px-2 py-4 text-left',
    'transition-colors duration-fast ease-standard',
    active && 'bg-accent/8',
    className
  );

  // 点亮态走 `highlight` token，不是一个**没有名字的浓度**（如 `bg-ink/[0.035]`）。
  // 「一个角色一个有名字的 token」正是为这种值立的。它同时是个正确性问题：3.5% 的墨压在
  // 深墨底板上对比 1.09，等于没有点亮。`highlight` 在两张纸上各有一只量过的值
  // （底板那只对底板 1.28）。
  const litGround = 'hover:bg-highlight';

  if (!onClick) {
    return (
      <m.div variants={staggerItem} data-testid={testId} className={shape}>
        {body}
      </m.div>
    );
  }

  return (
    <m.div variants={staggerItem}>
      <button
        type="button"
        data-testid={testId}
        aria-label={label}
        onClick={onClick}
        className={cn(shape, !active && litGround)}
      >
        {body}
      </button>
    </m.div>
  );
};

/**
 * 行右侧的读数（条数、金额、天数）—— 11px，**不走等宽字距那一套**。
 *
 * 11px 那一挡里其实是两样东西：`READOUT_LABEL`（等宽 + 0.12em 字距 +
 * 全大写）与「`标签 · 值` 读数」（`午后有雨 · 23° / 17°`、`2 个地点 · 1 条路线`）。
 * 前者是给**纯拉丁/纯数字**的，后者带中文。错用前者会让 `21 段` 印出来
 * 是 `21  段` —— 0.12em 的字距加在最后那个汉字后面把量词推开一格，而 `uppercase`
 * 对中文是空操作。**等宽/字距那一套排版给中文用就是错的。**
 *
 * 纯数字的值（时刻 `17:25`）该用 `READOUT_LABEL`，由调用点自己写 —— 那里等宽是对的，
 * 一列时刻要对齐。哪天再有一屏要排纯数字列，直接写 `READOUT_LABEL`，别再造一个包这一套
 * 的原语 —— 零消费方的原语和过滤空集的过滤器是同一个形状。
 */
export const RuledReadout: React.FC<{ children: React.ReactNode; className?: string }> = ({
  children,
  className,
}) => (
  <span className={cn('text-[11px] tabular-nums text-ink-muted', className)}>{children}</span>
);

import React from 'react';
import { cn } from '../../lib/utils';

/**
 * 字段 —— **一条刻线，不是一口井**。
 *
 * 规矩一句话：**一条线承一个值，一口井承一段话。**
 *
 * **不要**把每一个字段都做成 `rounded-card border border-stroke bg-surface` 的填色井：
 * 一张卡里套六口井 + 八枚带框芯片，一屏就有十几件带框/填色的元件、五种底色、四层嵌套，
 * 而 §Layout 明禁卡套卡。那种嵌套唯一的可见症状是 `index.css` 的
 * `.rounded-card .rounded-card` 把井自动降到 4px —— 也就是说合同写的档位根本没上线。
 *
 * - 单行的值（地点、日期、人数、搜索、一条记忆）走 `Input` / `Field`：一条 1px 发丝
 *   底线，无填色、无边框、无圆角。
 * - 多行的散文（自然语言想法、资料正文、输入区）走 `TextArea`：**保留暖井**，那口底色
 *   本身就在说「这里可以写句子」。
 *
 * 焦点态是**这条线自己**：一条 1.5px 的 accent 线淡入压在发丝线上。只动 opacity（§Motion
 * 「Animate only transform and opacity」），所以既不换行高也不撑边框宽度。没有盒子的时候
 * 线就是焦点可供性 —— 而输入框的焦点反馈正是明确保留的那一项。
 *
 * 命中区靠 `min-h-11`（44px）给，视觉上只是一条线。
 * 边框一律用整值 `border-stroke`，**不用** `/50`、`/30`、`/20` 那类半透明档：半透明描边的
 * 实际颜色由背后那一层决定，同一个 `Input` 放在纸白卡上和放在页面底色上会是两个色。
 */

/** 刻线字段的躯体。`Field` 与 `Input` 共用。 */
export const RULED_FIELD = cn(
  'relative flex min-h-11 w-full items-center gap-2 border-b border-stroke',
  // accent 那条线：绝对定位压在发丝线上，只淡入淡出。
  'after:pointer-events-none after:absolute after:inset-x-0 after:-bottom-px after:h-[1.5px]',
  'after:bg-accent after:opacity-0 after:transition-opacity after:duration-fast after:ease-standard',
  'focus-within:after:opacity-100'
);

/** 字段标签。12px/500，坐在线的上方。 */
/**
 * 线上方那个标签。**13px**，§Typography 的「Compact labels」那一挡。
 *
 * **不要压到 `text-xs`（12px）**，那是「Badges, metadata」那一挡 —— 一个字段的名字不是
 * 元数据，它是操作面上你第一眼要读的东西。字段标签、行内钮、芯片全落到类型表地板上，
 * 整个次级层就读不出层次，而操作屏并不缺宽度。
 */
export const FIELD_LABEL = 'mb-1 block text-[13px] font-medium text-ink-secondary';

/** 线上那个值。15px —— §Typography 专门为「Field values」立的那一挡。 */
export const FIELD_VALUE = 'text-[15px] text-ink placeholder:text-ink-muted';

/**
 * 一格带标签的刻线字段。内容（`<input>`、一段只读的值、一排 ± 钮）由调用方给。
 */
export const Field: React.FC<{
  label: string;
  /** 出错时线变 error 色并常显。 */
  invalid?: boolean;
  className?: string;
  children: React.ReactNode;
}> = ({ label, invalid, className, children }) => (
  <div className={cn('min-w-0 flex-1', className)}>
    <label className={FIELD_LABEL}>{label}</label>
    <div className={cn(RULED_FIELD, invalid && 'border-error after:bg-error after:opacity-100')}>
      {children}
    </div>
  </div>
);

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  icon?: React.ReactNode;
  /** 图标的定位覆盖（侧栏要把它推到轨道那条字形槽上）。 */
  iconClassName?: string;
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, icon, iconClassName, ...props },
  ref
) {
  return (
    <div className={RULED_FIELD}>
      {icon && (
        <span
          className={cn(
            'pointer-events-none absolute top-1/2 -translate-y-1/2 text-ink-muted',
            iconClassName || 'left-0'
          )}
        >
          {icon}
        </span>
      )}
      <input
        ref={ref}
        className={cn(
          'min-w-0 flex-1 border-none bg-transparent py-2',
          FIELD_VALUE,
          icon && !iconClassName ? 'pl-7' : '',
          className
        )}
        {...props}
      />
    </div>
  );
});

interface TextAreaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {}

/**
 * 多行散文那口井 —— 全站唯一保留填色的字段形态。底色说的是「这里写句子」。
 */
export const TextArea: React.FC<TextAreaProps> = ({ className, ...props }) => {
  return (
    <textarea
      className={cn(
        'w-full resize-none rounded-card border border-stroke bg-surface px-4 py-3',
        'text-[15px] text-ink placeholder:text-ink-muted',
        'transition-colors duration-base ease-standard',
        className
      )}
      {...props}
    />
  );
};

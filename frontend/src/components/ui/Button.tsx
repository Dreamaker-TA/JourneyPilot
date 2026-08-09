import React from 'react';
import { cn } from '../../lib/utils';

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'icon';
type ButtonSize = 'sm' | 'md' | 'lg';

interface ButtonBaseProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  size?: ButtonSize;
  loading?: boolean;
  children: React.ReactNode;
}

/**
 * `variant="icon"` **在类型上要求 `aria-label`**。
 *
 * 这一档的存在意义就是「只有字形、没有可见文字」——也就是说它的可及名**只能**由
 * `aria-label` 给，没有第二个来源。若它是可选的，三个使用点（弹层的 ✕、会话行的改名、
 * 侧栏的展开/收起）就会一个名字都没有：读屏软件把三枚同名说成「按钮」。类型约束让这类
 * 缺失在编译期原地报出来 —— 人工审计会漏。
 *
 * 名字**不**从 `ui/Tooltip` 来（理由写在那个文件的头注释里：它的 `content` 有时是说明
 * 而不是名字，而 `cloneElement` 到函数组件上会静默丢掉）。一枚控件的名字只有一处，
 * 就是它自己。
 */
type ButtonProps =
  | (ButtonBaseProps & { variant?: Exclude<ButtonVariant, 'icon'> })
  | (ButtonBaseProps & { variant: 'icon'; 'aria-label': string });

const variantClasses: Record<ButtonVariant, string> = {
  // `text-on-accent`，不是 `text-white`：白色不在这套调色板的十二只命名色里，
  // 而深墨底板上 accent 换成亮蓝之后白字只有 1.98 —— 一只写死的颜色不参与换档。
  primary:
    'bg-accent text-on-accent hover:bg-accent-hover active:bg-accent-hover shadow-sm',
  secondary:
    'bg-surface text-ink hover:bg-accent-soft active:bg-accent-soft',
  ghost:
    'bg-transparent text-ink-secondary hover:bg-ink/5 hover:text-ink',
  icon:
    'bg-transparent text-ink-secondary hover:bg-ink/10 hover:text-ink flex items-center justify-center',
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-xs rounded-card gap-1.5',
  md: 'h-10 px-4 text-sm rounded-card gap-2',
  lg: 'h-12 px-6 text-base rounded-card gap-2',
};

const iconSizeClasses: Record<ButtonSize, string> = {
  sm: 'h-7 w-7 rounded-card',
  md: 'h-9 w-9 rounded-card',
  lg: 'h-11 w-11 rounded-card',
};

export const Button: React.FC<ButtonProps> = ({
  variant = 'primary',
  size = 'md',
  loading = false,
  disabled,
  className,
  children,
  ...props
}) => {
  const isIcon = variant === 'icon';

  return (
    <button
      className={cn(
        'inline-flex items-center justify-center font-medium',
        // Shadows are static per variant (no hover-shadow state), so they stay
        // out of the transition list — animating box-shadow is forbidden.
        'transition-[transform,opacity,background-color,color,border-color] duration-fast ease-standard',
        // Press feedback: a subtle scale-down on active,
        // pure CSS so high-frequency taps stay on the compositor.
        'active:scale-[0.98]',
        'cursor-pointer select-none border-none',
        'disabled:opacity-50 disabled:pointer-events-none disabled:active:scale-100',
        variantClasses[variant],
        isIcon ? iconSizeClasses[size] : sizeClasses[size],
        className
      )}
      disabled={disabled || loading}
      {...props}
    >
      {loading && (
        <span className="h-4 w-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
      )}
      {children}
    </button>
  );
};

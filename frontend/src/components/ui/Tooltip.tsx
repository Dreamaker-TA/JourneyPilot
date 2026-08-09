import React, { useState, useRef } from 'react';
import { cn } from '../../lib/utils';

/**
 * 悬停微释义 —— 一层**纯视觉**的回声。
 *
 * **它不负责任何控件的可及名。**
 *
 * 探查时试过让它负责：`content` 就是那句可见标签，`cloneElement` 无条件接成子元素的
 * `aria-label`。两条实测理由否掉了这个方向：
 *
 * 1. **`content` 并不总是名字。** 全站 8 个调用点里，输入框下方那两枚（「补全行程描述」
 *    「整理较早对话」）自己带可见文字，tooltip 那句是**补充说明**；把它接成 `aria-label`
 *    会用一句和屏幕上不一样的话顶掉按钮真正的名字 —— 读屏听到的和眼睛看到的对不上。
 *    一个 prop 同时当「名字」和「说明」两用，没有干净的语义。
 * 2. **`cloneElement` 会静默失败。** `Hallmark` 包的是 `HallmarkTrigger`、会话行包的是
 *    `ConfirmAction` —— 函数组件不会把不认识的 prop 转发到自己的 `<button>` 上，于是
 *    aria-label 落在一个没人读的 props 对象里。「看起来接上了、其实没有」是这个仓最贵的
 *    那类失败。
 *
 * 所以**名字只写在控件自己身上**（`aria-label`），一处一份：`ui/Button` 的 `icon` 档在
 * **类型上**要求它（图标钮的存在意义就是没有可见文字，所以那一档不许无名），裸 `<button>`
 * 的图标触发器也各算一份。需要 tooltip 那句和名字一样时，在调用点提一个局部常量、
 * 两处消费同一份值。
 */
interface TooltipProps {
  content: string;
  children: React.ReactNode;
  position?: 'top' | 'bottom' | 'left' | 'right';
  delay?: number;
  disabled?: boolean;
  /**
   * 包裹层的类名。默认 `inline-flex`（跟着内容收），所以包一个 `w-full` 的控件时
   * 宽度会算成内容宽 —— 侧栏那枚满宽的「新建行程」必须把 `w-full` 也给到这一层。
   */
  className?: string;
}

export const Tooltip: React.FC<TooltipProps> = ({
  content,
  children,
  position = 'top',
  delay = 300,
  disabled = false,
  className,
}) => {
  const [visible, setVisible] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();

  const show = () => {
    if (disabled) return;
    timerRef.current = setTimeout(() => setVisible(true), delay);
  };
  const hide = () => {
    clearTimeout(timerRef.current);
    setVisible(false);
  };

  const positionClasses = {
    top: 'bottom-full left-1/2 -translate-x-1/2 mb-2',
    bottom: 'top-full left-1/2 -translate-x-1/2 mt-2',
    left: 'right-full top-1/2 -translate-y-1/2 mr-2',
    right: 'left-full top-1/2 -translate-y-1/2 ml-2',
  };

  return (
    <div
      className={cn('relative inline-flex', className)}
      onMouseEnter={show}
      onMouseLeave={hide}
    >
      {children}
      {visible && !disabled && (
        <div
          className={cn(
            'absolute z-50 px-2.5 py-1.5 text-xs font-medium',
            'bg-ink text-white rounded-card shadow-lg',
            'whitespace-nowrap pointer-events-none',
            // Tooltip is a high-frequency hover echo — retime the shared fade
            // keyframe to --dur-fast (hover echoes are fast).
            'animate-fade-in [animation-duration:var(--dur-fast)]',
            positionClasses[position]
          )}
        >
          {content}
        </div>
      )}
    </div>
  );
};

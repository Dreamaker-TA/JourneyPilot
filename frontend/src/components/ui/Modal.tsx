import React, { useEffect, useRef } from 'react';
import { m } from 'motion/react';
import { cn } from '../../lib/utils';
import { fadeIn, slideUp } from '../../lib/motion';
import { X } from 'lucide-react';
import { Button } from './Button';
import { useOverlayDismiss } from '../../hooks/useOverlayDismiss';

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
  maxWidth?: string;
}

export const Modal: React.FC<ModalProps> = ({
  open,
  onClose,
  title,
  children,
  maxWidth = 'max-w-lg',
}) => {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [open]);

  /**
   * Esc 关闭 + 焦点归还 —— **一处定义，就在这个原语里**，四个使用点一行都不写。
   *
   * `role="dialog"` + `aria-modal="true"` 是一个承诺，而承诺里就含着「Esc 能出来」。
   */
  useOverlayDismiss({ open, onDismiss: onClose, panelRef });

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <m.button
        type="button"
        data-testid="modal-scrim"
        /**
         * 遮罩对辅助技术**隐身**。
         *
         * 它是 `<button>`（点击关闭需要一个真的可点元素），但既不可 Tab 到、也没有名字：
         * 给它编一个名字（「关闭」）会让同一个动作在这一屏有三个可及入口（Esc、✕、遮罩），
         * 其中一个还是一整块看不见的东西。
         *
         * 所以它是**纯指针可供性**：`aria-hidden` + `tabIndex={-1}`。Esc 与 ✕ 都在，
         * 关闭这件事对键盘与读屏用户一点没少。
         */
        aria-hidden="true"
        tabIndex={-1}
        className="absolute inset-0 cursor-default bg-ink/25 backdrop-blur-sm"
        onClick={onClose}
        variants={fadeIn}
        initial="hidden"
        animate="visible"
      />
      <m.div
        ref={panelRef}
        /**
         * `tabIndex={-1}`：打开时焦点要落进面板（`useOverlayDismiss`），而一个 `div`
         * 默认不可聚焦 —— 少了这一行 `focus()` 无声失效，Tab 仍然从页面顶部开始。
         */
        tabIndex={-1}
        /**
         * `role="dialog"` + `aria-modal`。
         *
         * **弹层角色全站一处定义，就在这里**：业务组件一行 role 都不写。焦点归还与
         * Esc 关闭也在一处定义，就是 `hooks/useOverlayDismiss`；关闭有三条路：Esc、✕、
         * 遮罩点击。
         *
         * **焦点陷阱（Tab 循环）没有实现**，这是一句实话而不是遗漏的托词：
         * `aria-modal="true"` 已经让读屏软件的虚拟光标留在这枚面板内；Tab 走出面板之后
         * 再按 Esc 仍然关得掉、焦点仍然归还 —— 差的是「Tab 到最后一枚之后回到第一枚」。
         * 要补它就得在这里再造一份「哪些元素可聚焦」的名单，那份名单是整个无障碍工程里
         * 最容易和真实 DOM 漂开的一张表。
         *
         * 可及名取 `title`：没有 title 的弹层就没有可及名，不在这里编一个。
         */
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={cn(
          'relative w-full rounded-card border border-stroke bg-panel p-6 shadow-lg',
          'max-h-[min(90vh,800px)] overflow-y-auto overscroll-contain',
          maxWidth,
          'mx-auto shadow-lg'
        )}
        variants={slideUp}
        initial="hidden"
        animate="visible"
      >
        {title && (
          <div className="flex items-center justify-between mb-4 gap-2">
            <h3 className="text-base font-semibold text-ink">
              {title}
            </h3>
            <Button variant="icon" size="sm" aria-label="关闭" onClick={onClose}>
              <X size={16} />
            </Button>
          </div>
        )}
        {children}
      </m.div>
    </div>
  );
};

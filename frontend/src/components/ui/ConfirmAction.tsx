import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AnimatePresence, m } from 'motion/react';
import { Loader2 } from 'lucide-react';
import { cn } from '../../lib/utils';
import { duration, easing, transitions } from '../../lib/motion';

/**
 * In-place two-step confirm — the destructive-action T2 grammar.
 *
 * A single trigger button that, on click, morphs in place into a "确认 / 取消"
 * pair: the trailing confirm/cancel controls grow out of the trigger's right
 * edge (`width 0 → auto` on `base` + `decelerate`; the reverse collapse runs on
 * `fast` + `accelerate`). If left untouched for 4s the control reverts on its
 * own; clicking 取消 reverts immediately; clicking 确认 fires `onConfirm`.
 * Every single-object delete/overwrite shares this one body language rather than
 * a native browser confirm dialog.
 *
 * Pure click interaction by design. The width tween animates via
 * variants (not the `layout` prop) so it runs on the main-bundle `domAnimation`
 * feature set without pulling in domMax.
 */
export interface ConfirmActionProps {
  /** Resting-state trigger content (icon + label). */
  children: React.ReactNode;
  /** Runs when the user confirms. */
  onConfirm: () => void;
  /** Confirm control label. Default "确认". */
  confirmLabel?: string;
  /** Cancel control label. Default "取消". */
  cancelLabel?: string;
  /** Auto-revert delay in ms after entering the armed state. Default 4000. */
  revertAfterMs?: number;
  disabled?: boolean;
  /**
   * Async pending flag. While true the armed pair holds open, the confirm
   * control shows a spinner and both controls disable, and the 4s auto-revert
   * timer is suspended — the caller owns dismissal (usually by unmounting the
   * primitive once the operation resolves).
   */
  confirmPending?: boolean;
  /** Test hook on the trigger button. */
  testId?: string;
  /** Tone of the confirm control. Default "error" (destructive). */
  tone?: 'error' | 'accent';
  /** Extra classes on the outer shell. */
  className?: string;
  /** Extra classes on the resting trigger button (e.g. coarse-pointer sizing). */
  triggerClassName?: string;
  /**
   * 触发器的可及名。
   *
   * `children` 是触发器的可见内容。当它只有一枚字形（会话行的删除钮就是 `<Trash2/>`）时，
   * 这枚钮对读屏软件**没有名字**，而它是一个破坏性动作 —— 听不出名字的破坏性动作是这一族
   * 里最贵的一个。带可见文字的触发器不要传这个：那会让同一枚钮有两份名字，而屏幕上那一份
   * 才是真的。
   */
  triggerLabel?: string;
}

// Resting trigger hover: the whole pill deepens as one surface. The shell owns
// the hover fill via a scoped `group`, so the tint covers the pill edge-to-edge.
// Putting it on the inner h-7 button instead stacks a second translucent layer
// and leaves the shell's px padding as a lighter ring. The resting trigger
// therefore carries text color only; the armed confirm/cancel controls keep
// their own local `control` hover.
const toneClasses = {
  error: {
    shell: 'border-error/20 bg-error/10 text-error hover:bg-error/[0.18]',
    trigger: 'text-error',
    control: 'text-error hover:bg-error/10 hover:text-error',
  },
  accent: {
    shell: 'border-accent/20 bg-accent/10 text-accent hover:bg-accent/[0.18]',
    trigger: 'text-accent',
    control: 'text-accent hover:bg-accent/10 hover:text-accent',
  },
} as const;

export const ConfirmAction: React.FC<ConfirmActionProps> = ({
  children,
  onConfirm,
  confirmLabel = '确认',
  cancelLabel = '取消',
  revertAfterMs = 4000,
  disabled,
  confirmPending = false,
  testId,
  tone = 'error',
  className,
  triggerClassName,
  triggerLabel,
}) => {
  const [armed, setArmed] = useState(false);
  // Flipped true the moment confirm fires; gates the post-confirm collapse below
  // so the decision to hold open (pending) vs. collapse (sync) is made on the
  // next render — after the parent has had a chance to flip `confirmPending` —
  // not in the stale click closure. State (not a ref) so the collapse effect
  // actually re-runs after a synchronous confirm.
  const [fired, setFired] = useState(false);
  const timerRef = useRef<number | null>(null);
  const t = toneClasses[tone];

  const clearTimer = useCallback(() => {
    if (timerRef.current != null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const disarm = useCallback(() => {
    if (confirmPending) return;
    clearTimer();
    setFired(false);
    setArmed(false);
  }, [clearTimer, confirmPending]);

  const arm = useCallback(() => {
    setFired(false);
    setArmed(true);
    clearTimer();
    timerRef.current = window.setTimeout(() => setArmed(false), revertAfterMs);
  }, [clearTimer, revertAfterMs]);

  const confirm = useCallback(() => {
    // Hold the pair open on the click; the post-confirm effect decides whether to
    // collapse (synchronous consumer) or stay open (async, `confirmPending`).
    clearTimer();
    setFired(true);
    onConfirm();
  }, [clearTimer, onConfirm]);

  // Post-confirm collapse. While `confirmPending` is true the pair stays open
  // (in-flight) and the auto-revert timer is suspended. Once pending is false
  // after a confirm fired — immediately for synchronous consumers, or when the
  // async op resolves — the pair collapses back to the resting trigger.
  useEffect(() => {
    if (confirmPending) {
      clearTimer();
      return;
    }
    if (fired) {
      setFired(false);
      setArmed(false);
    }
  }, [confirmPending, fired, clearTimer]);

  useEffect(() => clearTimer, [clearTimer]);

  return (
    <div
      /* 二段确认**就是**一枚按钮原地长出来：旁边那一行要跟着重排，没有等价的
         transform 能做到 —— 所以这条 width 过渡必须真的发生，不是靠类名猜的装饰。 */
      data-confirm-action=""
      className={cn(
        'inline-flex items-center gap-1 rounded-card border px-2 py-1 transition-colors duration-base ease-standard',
        t.shell,
        className
      )}
    >
      {/**
       * 待发态的触发器 —— **armed 之后它让位**。
       *
       * 「点击后按钮**就地变形**为「确认 / 取消」双钮」——「变形」就含着触发器该消失：
       * 让触发器留在原地，屏幕上会并排三枚钮、其中两枚印着同一个词（「删除　删除　取消」），
       * 读者得先分辨哪一个才是真的那一下。
       *
       * 节点不删、只 `hidden`：两枚钮在 DOM 里都得在（有的消费方先点触发器、再点确认钮）。
       * 而且它是**瞬时**退场，不做过渡 —— 「geometry snaps; only appearance animates」：
       * 让位是几何，运动由旁边那对钮自己的宽度动画承担。**不要**给触发器也加
       * `width`/`padding` 过渡：那会新造两个布局动画。
       */}
      <button
        type="button"
        data-testid={testId}
        aria-label={triggerLabel}
        disabled={disabled || confirmPending}
        onClick={() => (armed ? disarm() : arm())}
        className={cn(
          // 触摸热区契约（≥44px）由 `index.css` 按元素给，这里不写类名。
          'inline-flex h-7 items-center gap-1.5 rounded-card px-2.5 text-xs font-medium',
          'transition-colors duration-base ease-standard',
          'disabled:pointer-events-none disabled:opacity-50',
          armed && 'hidden',
          t.trigger,
          triggerClassName
        )}
      >
        {children}
      </button>
      <AnimatePresence initial={false}>
        {armed && (
          <m.div
            key="confirm-pair"
            initial={{ opacity: 0, width: 0 }}
            animate={{
              opacity: 1,
              width: 'auto',
              transition: {
                width: transitions.enter(duration.base),
                opacity: { duration: duration.fast, ease: easing.decelerate },
              },
            }}
            /* 还原走 `fast + accelerate`，**必须写在 `exit` 自己身上**：`exit` 不带
               transition 时会继承元素级那一条，也就是入场的 decelerate 曲线，收起于是
               用展开那条曲线、慢一倍。 */
            exit={{
              opacity: 0,
              width: 0,
              transition: {
                width: transitions.exit(duration.fast),
                opacity: { duration: duration.fast, ease: easing.accelerate },
              },
            }}
            className="flex items-center gap-1 overflow-hidden"
          >
            <button
              type="button"
              data-testid={testId ? `${testId}-confirm` : undefined}
              onClick={confirm}
              disabled={confirmPending}
              className={cn(
                'inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-card px-2.5 text-xs font-semibold',
                'transition-colors duration-fast ease-standard',
                'disabled:pointer-events-none',
                t.control
              )}
            >
              {confirmPending && <Loader2 size={12} className="animate-spin" />}
              {confirmLabel}
            </button>
            {!confirmPending && (
              <button
                type="button"
                data-testid={testId ? `${testId}-dismiss` : undefined}
                onClick={disarm}
                className={cn(
                  'inline-flex h-7 items-center whitespace-nowrap rounded-card px-2.5 text-xs font-medium text-ink-secondary',
                  'transition-colors duration-fast ease-standard hover:bg-ink/5 hover:text-ink'
                )}
              >
                {cancelLabel}
              </button>
            )}
          </m.div>
        )}
      </AnimatePresence>
    </div>
  );
};

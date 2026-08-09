import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { PanInfo } from 'motion/react';
import { AnimatePresence, animate, m, useDragControls, useMotionValue } from 'motion/react';
import { cn } from '../../lib/utils';
import { duration, easing, spring } from '../../lib/motion';
import { CanvasMotion } from '../motion/CanvasMotion';
import { useOverlayDismiss } from '../../hooks/useOverlayDismiss';

/**
 * 底部浮层原语 —— 移动端画布与重内容的容器。
 *
 * 交互契约（Toss × Apple sheet 纪律）：
 * - detent 停靠 55% / 92%（视口高度百分比）：55 是预览位，92 是工作位；
 * - 顶部拖拽把手（44px 热区）：拖拽跟手，松手按「速度 + 位置」吸附最近 detent，
 *   轻点把手在两档间切换；
 * - 下滑超阈值、点 scrim、按 Esc 关闭；内容区滚动与拖拽互不抢手（拖拽只由把手发起）。
 *
 * Esc 与焦点归还走 `hooks/useOverlayDismiss`。「手机上没有键盘」不是理由：同一份
 * CSS/JS 在带键盘的触摸设备（iPad + 键盘、Windows 触屏笔电）上一样跑，而那些设备正是
 * coarse 指针档覆盖的对象。
 *
 * 动效规格（全部既有 token）：入场 `slow`+`decelerate` 自底滑入；detent 吸附
 * `spring`（空间移动专用）；出场 `base`+`accelerate`。
 *
 * 拖拽手势需要 domMax：面板 m.div 挂在 `CanvasMotion` 异步边界内复用画布的
 * domMax chunk，主 bundle 零增量。
 */

export type SheetDetent = 55 | 92;

interface SheetProps {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  /** 打开时的初始 detent；默认 55（预览位）。 */
  initialDetent?: SheetDetent;
  testId?: string;
}

/** 松手后按当前速度外推的时间窗（s）——把「甩」的意图折算成位置再取最近锚点。 */
const PROJECTION_WINDOW_S = 0.2;
/** 快速甩动阈值（px/s）：超过即视为明确的方向指令，一次甩一档（Apple sheet 惯例）。 */
const FLICK_VELOCITY = 900;

function useViewportHeight(): number {
  const [vh, setVh] = useState(() => (typeof window === 'undefined' ? 800 : window.innerHeight));
  useEffect(() => {
    const update = () => setVh(window.innerHeight);
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  }, []);
  return vh;
}

export const Sheet: React.FC<SheetProps> = ({
  open,
  onClose,
  children,
  initialDetent = 55,
  testId = 'sheet',
}) => {
  const vh = useViewportHeight();
  // 面板高度固定为最大 detent（92%），detent 切换只平移不重排——拖拽全程合成器。
  const panelHeight = Math.round(vh * 0.92);
  const yFor = useCallback(
    (detent: SheetDetent) => panelHeight - Math.round(vh * (detent / 100)),
    [panelHeight, vh]
  );
  const closedY = panelHeight;

  const [detent, setDetent] = useState<SheetDetent>(initialDetent);
  const [entered, setEntered] = useState(false);
  const y = useMotionValue(closedY);
  const dragControls = useDragControls();
  // 把手「拖拽后紧跟的 click」抑制：拖过就不当 tap 处理。
  const draggedRef = useRef(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // Esc 关闭 + 焦点归还（打开它的那枚药丸），全站一处定义。
  useOverlayDismiss({ open, onDismiss: onClose, panelRef });

  // 重新打开时回到初始 detent、重放入场。
  useEffect(() => {
    if (open) {
      setDetent(initialDetent);
      setEntered(false);
    }
  }, [open, initialDetent]);

  const snapTo = useCallback(
    (target: SheetDetent) => {
      if (target === detent) {
        // animate 目标未变时声明式不会重跑——就地用 spring 收回当前 detent。
        animate(y, yFor(target), spring);
      } else {
        setDetent(target);
      }
    },
    [detent, y, yFor]
  );

  const handleDragEnd = useCallback(
    (_event: MouseEvent | TouchEvent | PointerEvent, info: PanInfo) => {
      const position = y.get();
      const velocity = info.velocity.y;

      // 明确的甩动：一次一档。向下甩在预览位（55）= 关闭，在工作位（92）= 降到预览位。
      if (velocity > FLICK_VELOCITY) {
        if (detent === 55) {
          onClose();
        } else {
          snapTo(55);
        }
        return;
      }
      if (velocity < -FLICK_VELOCITY) {
        snapTo(92);
        return;
      }

      // 慢速松手：位置 + 速度外推，取最近锚点（92 位 / 55 位 / 关闭）。
      const projected = position + velocity * PROJECTION_WINDOW_S;
      const anchors: Array<{ y: number; resolve: () => void }> = [
        { y: yFor(92), resolve: () => snapTo(92) },
        { y: yFor(55), resolve: () => snapTo(55) },
        { y: closedY, resolve: onClose },
      ];
      anchors.reduce((best, anchor) =>
        Math.abs(anchor.y - projected) < Math.abs(best.y - projected) ? anchor : best
      ).resolve();
    },
    [closedY, detent, onClose, snapTo, y, yFor]
  );

  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-50">
          <CanvasMotion>
            <m.div
              data-testid="sheet-scrim"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0, transition: { duration: duration.fast, ease: easing.accelerate } }}
              transition={{ duration: duration.base, ease: easing.decelerate }}
              className="absolute inset-0 cursor-default bg-ink/25 backdrop-blur-sm"
              onClick={onClose}
            />
            <m.div
              ref={panelRef}
              /* 面板要能接住焦点（见 `useOverlayDismiss`）：`div` 默认不可聚焦，
                 少了这一行 `focus()` 无声失效。 */
              tabIndex={-1}
              data-testid={testId}
              data-detent={detent}
              initial={{ y: closedY }}
              animate={{ y: yFor(detent) }}
              exit={{ y: closedY, transition: { duration: duration.base, ease: easing.accelerate } }}
              transition={
                entered
                  ? { y: spring }
                  : { y: { duration: duration.slow, ease: easing.decelerate } }
              }
              onAnimationComplete={() => setEntered(true)}
              drag="y"
              dragListener={false}
              dragControls={dragControls}
              dragConstraints={{ top: 0, bottom: closedY }}
              dragElastic={{ top: 0.04, bottom: 0 }}
              dragMomentum={false}
              onDragStart={() => {
                draggedRef.current = true;
              }}
              onDragEnd={handleDragEnd}
              style={{ y, height: panelHeight }}
              className={cn(
                'absolute inset-x-0 bottom-0 flex flex-col overflow-hidden border-t border-stroke bg-panel shadow-lg',
                // 半径走角色名 `rounded-t-card`，**不要**写成 `rounded-t-[var(--radius-*)]`：
                // 半径 token 是角色命名的，尺寸名的变量并不存在，而一个未定义的 CSS 变量会让
                // 整条 `border-radius` 静默失效（上两角变直角，没有任何报错）。`theme.borderRadius`
                // 被整张替换过，就是为了让漏改的调用点显式坏掉；方括号里的任意值绕过那道门。
                'rounded-t-card pb-[env(safe-area-inset-bottom)]'
              )}
            >
              <div
                data-testid="sheet-handle"
                onPointerDown={(event) => {
                  draggedRef.current = false;
                  dragControls.start(event);
                }}
                onClick={() => {
                  if (draggedRef.current) {
                    draggedRef.current = false;
                    return;
                  }
                  snapTo(detent === 55 ? 92 : 55);
                }}
                className="flex h-11 flex-none cursor-grab touch-none items-center justify-center active:cursor-grabbing"
              >
                <div className="h-1 w-9 rounded-full bg-ink/15" />
              </div>
              <div className="min-h-0 flex-1">{children}</div>
            </m.div>
          </CanvasMotion>
        </div>
      )}
    </AnimatePresence>
  );
};

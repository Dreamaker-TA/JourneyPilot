import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, m } from 'motion/react';
import type { Variants } from 'motion/react';
import { cn } from '../../lib/utils';
import { duration, easing, transitions } from '../../lib/motion';
import { useOverlayDismiss } from '../../hooks/useOverlayDismiss';

/**
 * Anchored, in-place popover primitive.
 *
 * Neither Tooltip (pointer-events-none, text only) nor Modal (full-screen)
 * fits the hallmark "grow out of the anchor" interaction. This is a non-modal
 * layer that:
 *
 * - grows from the trigger corner: `transformOrigin` is the anchored corner,
 *   entrance is `scale 0.96 → 1 + opacity 0 → 1` on the **restrained** axis
 *   (`slow` + `decelerate` — a surface arriving); exit runs the reverse on
 *   `base` + `accelerate` via `AnimatePresence`.
 *
 *   Never move this to the expressive axis (`transitions.emphasisEnter`). That
 *   axis has exactly two sanctioned moments — the itinerary revealing, an
 *   approval gate appearing — and a popover opened on the itinerary-reveal
 *   screen would put two overshoots in one frame. A popover is a surface, not
 *   a moment.
 * - is 320–360px wide, flips inward when it would overflow the viewport, and on
 *   narrow screens clamps to the host container's width.
 * - closes on outside-click; re-clicking the trigger toggles it.
 *
 * The consumer supplies the trigger via a render prop so it can wire the ref and
 * toggle handler onto its own element.
 */

/** Corner of the trigger the panel is anchored to; drives `transformOrigin`. */
export type PopoverPlacement = 'bottom-start' | 'bottom-end' | 'top-start' | 'top-end';

interface TriggerRenderProps {
  ref: React.Ref<HTMLButtonElement>;
  open: boolean;
  toggle: () => void;
}

export interface PopoverProps {
  /** Render prop for the trigger. Spread the provided props onto a <button>. */
  trigger: (props: TriggerRenderProps) => React.ReactNode;
  /**
   * Popover body. May be static content, or a render prop that receives a
   * `close` callback so content (e.g. a select menu) can dismiss the popover on
   * a choice.
   */
  children: React.ReactNode | ((close: () => void) => React.ReactNode);
  /** Preferred anchoring corner; flips inward on overflow. Default bottom-start. */
  placement?: PopoverPlacement;
  /** Gap in px between trigger and panel. Default 8. */
  offset?: number;
  /**
   * Match the panel width to the trigger width instead of the 320–360px range.
   * Used by form-field consumers (SelectMenu) where the menu should track the
   * field it drops out of. Still clamped to the viewport as a mobile safety net.
   */
  matchTriggerWidth?: boolean;
  /** Extra classes on the panel surface. */
  className?: string;
  /**
   * Render the panel through a portal to `document.body` instead of in place.
   *
   * Anchored floaters that live inside `overflow-hidden` / `overflow-x-auto`
   * ancestors need this: an in-place `fixed` panel is still clipped by the
   * ancestor's overflow. The citation / annotation / transport-provider
   * markers all live in such containers, so they set `portal`.
   */
  portal?: boolean;
  /**
   * `closeOnEscape` 不是一个开关：Esc **无条件生效**，走 `hooks/useOverlayDismiss`。
   *
   * 同一个原语上不能一半实例按 Esc 能出、一半不能 —— 从界面上看不出区别，却是同一份
   * 契约两种兑现。
   */
  /** Optional `data-testid` on the panel surface. */
  testId?: string;
}

const MIN_WIDTH = 320;
const MAX_WIDTH = 360;
const VIEWPORT_MARGIN = 12;

/** transform-origin per resolved placement — the panel grows from the anchor corner. */
const ORIGIN: Record<PopoverPlacement, string> = {
  'bottom-start': 'top left',
  'bottom-end': 'top right',
  'top-start': 'bottom left',
  'top-end': 'bottom right',
};

interface Position {
  top: number;
  left: number;
  width: number;
  placement: PopoverPlacement;
}

/** Compute a fixed-position box, flipping inward and clamping to the viewport. */
function computePosition(
  triggerRect: DOMRect,
  panelHeight: number,
  preferred: PopoverPlacement,
  offset: number,
  matchTriggerWidth: boolean
): Position {
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  // Width. Default: sits in the [MIN, MAX] range, capped to the viewport.
  // Form-field mode (`matchTriggerWidth`): tracks the trigger so a select menu
  // is exactly as wide as the field it drops from. Both clamp to the viewport so
  // narrow screens never overflow. Authoritative — never derived from content,
  // so measurement never feeds back.
  const available = vw - VIEWPORT_MARGIN * 2;
  const width = matchTriggerWidth
    ? Math.min(triggerRect.width, available)
    : available <= MIN_WIDTH
      ? available
      : Math.min(MAX_WIDTH, available);
  const height = panelHeight;

  let vertical: 'top' | 'bottom' = preferred.startsWith('top') ? 'top' : 'bottom';
  let horizontal: 'start' | 'end' = preferred.endsWith('start') ? 'start' : 'end';

  // Vertical flip: prefer below, flip above when it would clip the bottom edge
  // and there is more room above.
  const spaceBelow = vh - triggerRect.bottom - offset;
  const spaceAbove = triggerRect.top - offset;
  if (vertical === 'bottom' && spaceBelow < height && spaceAbove > spaceBelow) {
    vertical = 'top';
  } else if (vertical === 'top' && spaceAbove < height && spaceBelow > spaceAbove) {
    vertical = 'bottom';
  }

  // Horizontal: 'start' aligns panel-left to trigger-left; 'end' aligns
  // panel-right to trigger-right. Flip the alignment when it overflows.
  let left = horizontal === 'start' ? triggerRect.left : triggerRect.right - width;
  if (horizontal === 'start' && left + width > vw - VIEWPORT_MARGIN) {
    horizontal = 'end';
    left = triggerRect.right - width;
  } else if (horizontal === 'end' && left < VIEWPORT_MARGIN) {
    horizontal = 'start';
    left = triggerRect.left;
  }
  // Final clamp so the panel is always fully on-screen (mobile safety net).
  left = Math.min(Math.max(left, VIEWPORT_MARGIN), vw - width - VIEWPORT_MARGIN);

  const top =
    vertical === 'bottom'
      ? triggerRect.bottom + offset
      : triggerRect.top - offset - height;

  return { top, left, width, placement: `${vertical}-${horizontal}` as PopoverPlacement };
}

export const Popover: React.FC<PopoverProps> = ({
  trigger,
  children,
  placement = 'bottom-start',
  offset = 8,
  matchTriggerWidth = false,
  className,
  portal = false,
  testId,
}) => {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<Position | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => setOpen(false), []);
  const toggle = useCallback(() => setOpen((v) => !v), []);

  // Position measurement: measure the panel then place it. Runs on open and on
  // resize/scroll so the panel tracks the anchor and re-flips on viewport change.
  const reposition = useCallback(() => {
    const t = triggerRef.current;
    const p = panelRef.current;
    if (!t || !p) return;
    const triggerRect = t.getBoundingClientRect();
    setPosition(computePosition(triggerRect, p.offsetHeight, placement, offset, matchTriggerWidth));
  }, [placement, offset, matchTriggerWidth]);

  useLayoutEffect(() => {
    if (!open) {
      // Keep the last position so the AnimatePresence exit stays anchored and
      // visible; it is recomputed on the next open.
      return undefined;
    }
    reposition();
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, true);
    return () => {
      window.removeEventListener('resize', reposition);
      window.removeEventListener('scroll', reposition, true);
    };
  }, [open, reposition]);

  // Outside-click closes. Trigger clicks are ignored here (the trigger's own
  // toggle handles them).
  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (panelRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      close();
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [open, close]);

  /**
   * Esc 关闭 + 焦点归还，走全站那一处定义。
   *
   * **不传 `panelRef`**：锚定浮层是非模态的，打开它不该把焦点从触发器上搬走。归还的
   * 对象由那个 hook 记（「打开之前焦点在哪」），所以这里也不再自己 `triggerRef.focus()`
   * —— 那是同一件事的第二份账。
   */
  useOverlayDismiss({ open, onDismiss: close });

  const resolvedPlacement = position?.placement ?? placement;

  // Entrance/exit variants: grow from the anchor corner (scale + opacity).
  // Restrained axis only — a dropdown/menu is not one of the three expressive
  // moments, so surface `slow`+`decelerate` on enter, `base`+`accelerate` on exit
  // (no `emphasis`/overshoot).
  const panelVariants: Variants = {
    hidden: { opacity: 0, scale: 0.96 },
    visible: {
      opacity: 1,
      scale: 1,
      transition: {
        opacity: { duration: duration.slow, ease: easing.decelerate },
        scale: { duration: duration.slow, ease: easing.decelerate },
      },
    },
    exit: {
      opacity: 0,
      scale: 0.96,
      transition: {
        opacity: transitions.exit(duration.base),
        scale: { duration: duration.base, ease: easing.accelerate },
      },
    },
  };

  const panel = (
    <AnimatePresence>
      {open && (
        <m.div
          key="popover-panel"
          ref={panelRef}
          data-testid={testId}
          className={cn('fixed z-50 rounded-card border border-stroke bg-panel', className)}
          style={{
            top: position?.top ?? 0,
            left: position?.left ?? 0,
            width: position?.width ?? MIN_WIDTH,
            transformOrigin: ORIGIN[resolvedPlacement],
            // 浮层用 md 挡阴影 —— 浮起面那一档。
            boxShadow: 'var(--shadow-md)',
            // Hide until positioned to avoid a first-frame flash at (0,0).
            visibility: position ? 'visible' : 'hidden',
          }}
          variants={panelVariants}
          initial="hidden"
          animate="visible"
          exit="exit"
        >
          {typeof children === 'function' ? children(close) : children}
        </m.div>
      )}
    </AnimatePresence>
  );

  return (
    <>
      {trigger({
        ref: triggerRef,
        open,
        toggle,
      })}
      {portal && typeof document !== 'undefined'
        ? createPortal(panel, document.body)
        : panel}
    </>
  );
};

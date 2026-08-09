/**
 * Motion tokens — the system layer for animation, peer to color and type.
 *
 * These values are the single source of truth. They are mirrored 1:1 as CSS
 * custom properties in `src/index.css` (`--dur-*`, `--ease-*`, `--stagger-step`)
 * so that plain CSS transitions and motion-library animations never drift apart.
 *
 * Calibration source: research:streaming-motion-ux/SYNTHESIS §2
 * (Carbon non-linear productive family + Vercel three-value scale + NN/g consensus).
 */

import type { Transition, Variants } from 'motion/react';

/** Durations in seconds (motion's native unit). */
export const duration = {
  fast: 0.12, // 120ms — high-frequency state, hover/focus echoes
  base: 0.2, // 200ms — everyday state changes, default
  slow: 0.32, // 320ms — enter/exit of surfaces, drawers
  emphasis: 0.48, // 480ms — expressive axis only (the TWO whitelisted moments)
  numberRoll: 0.45, // 450ms — odometer digit roll (RollingNumber), decelerate
  loop: 1.2, // 1.2s  — ambient PULSE: a breath or a turn in place (dots, rings, beacons)
  loopDrift: 2.4, // 2.4s — ambient DRIFT: continuous travel in one direction (the cruising plane, a sheen)
} as const;

/** Same durations in milliseconds — for CSS parity / display / non-motion use. */
export const durationMs = {
  fast: 120,
  base: 200,
  slow: 320,
  emphasis: 480,
  numberRoll: 450,
  loop: 1200,
  loopDrift: 2400,
} as const;

/**
 * A loop that has been running this long is not progress, it is a stall.
 *
 * §Motion Rules: "progress indicators must degrade to a stalled state on
 * timeout, never spin forever." This is the threshold that sentence needs and
 * never had. Not a CSS token — nothing animates for 12 seconds; it gates a
 * state change in JS.
 */
export const stallThresholdMs = 12_000;

/**
 * Cubic-bezier easing curves. Enter is long, exit is short:
 * pair `decelerate` with slow/emphasis for entrances, `accelerate` with
 * fast/base for exits.
 */
export const easing = {
  standard: [0.2, 0, 0.38, 0.9], // Carbon productive-standard — day-to-day state change
  decelerate: [0, 0, 0.38, 0.9], // entrances (arrives, settles)
  accelerate: [0.2, 0, 1, 0.9], // exits (leaves quickly)
} as const;

/**
 * Spring — spatial movement ONLY (layout / shared-element / reorder).
 * Size and position may overshoot; opacity and color never do.
 */
export const spring: Transition = {
  type: 'spring',
  stiffness: 300,
  damping: 24,
};

/** Stagger rhythm. Total orchestration time is hard-capped at `maxTotal`. */
export const stagger = {
  step: 0.04, // 40ms per item
  maxTotal: 0.48, // 480ms hard cap — items beyond the cap land on the same frame
} as const;

/**
 * Per-item stagger delay that keeps the whole list under the 480ms cap.
 * With many items the step shrinks so the sequence never runs long.
 */
export function staggerStep(count: number): number {
  if (count <= 1) return 0;
  return Math.min(stagger.step, stagger.maxTotal / count);
}

/** Reusable transition presets built from the tokens. */
export const transitions = {
  /** Everyday state change. */
  standard: (d: number = duration.base): Transition => ({ duration: d, ease: easing.standard }),
  /** Entrances — slower, decelerating. */
  enter: (d: number = duration.slow): Transition => ({ duration: d, ease: easing.decelerate }),
  /** Exits — quicker, accelerating. */
  exit: (d: number = duration.base): Transition => ({ duration: d, ease: easing.accelerate }),
  /** Spatial movement (layout / position). */
  spatial: spring,
  /**
   * Odometer digit roll (RollingNumber). Decelerating so each digit settles on
   * its target; ~450ms. When a new target arrives mid-roll the motion library
   * re-targets from the current position rather than queueing (§5.2 "重定向不堆积").
   */
  numberRoll: (): Transition => ({ duration: duration.numberRoll, ease: easing.decelerate }),
} as const;

// ─── Variant presets ──────────────────────────────────────────────────────
// Restrained axis (default). Only transform + opacity animate; opacity never
// overshoots.

// Both carry an `exit`: without one, an `AnimatePresence` child built from these
// variants leaves on the same frame it is unmounted (enter is long, exit is
// short — but "short" is not "absent"). `fadeIn` had no exit at all, which is
// why `Modal` could arrive and not leave.
export const fadeIn: Variants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1, transition: transitions.enter() },
  exit: { opacity: 0, transition: transitions.exit(duration.fast) },
};

export const slideUp: Variants = {
  hidden: { opacity: 0, y: 8 },
  visible: { opacity: 1, y: 0, transition: transitions.enter() },
  exit: { opacity: 0, y: -4, transition: transitions.exit(duration.fast) },
};

/**
 * Expressive axis — reserved for exactly **two** moments: the itinerary
 * revealing for the first time, and an approval gate appearing.
 *
 * The third, "plan ready", is not one: the itinerary
 * appearing *is* the plan being ready, so wiring it would fire a second
 * overshoot on the same screen in the same frame.
 */
export const emphasisEnter: Variants = {
  hidden: { opacity: 0, y: 14, scale: 0.985 },
  visible: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: {
      opacity: transitions.enter(duration.slow),
      y: { ...spring, duration: duration.emphasis },
      scale: { ...spring, duration: duration.emphasis },
    },
  },
  exit: {
    opacity: 0,
    y: -6,
    scale: 0.995,
    transition: transitions.exit(duration.fast),
  },
};

/** Item inside a staggered list — pair with a stagger container. */
export const staggerItem: Variants = {
  hidden: { opacity: 0, y: 8 },
  visible: { opacity: 1, y: 0, transition: transitions.enter(duration.base) },
  exit: { opacity: 0, y: -4, transition: transitions.exit(duration.fast) },
};

/**
 * Stagger container. Pass the child count so the per-item delay is capped to
 * keep the whole sequence within `stagger.maxTotal` (480ms). `initial={false}`
 * on the consumer avoids animating the first paint.
 */
export function staggerContainer(count = 0): Variants {
  return {
    hidden: {},
    visible: {
      transition: { staggerChildren: count > 0 ? staggerStep(count) : stagger.step },
    },
  };
}

/** Color/opacity pulse only — no looping, no spatial movement. */
export const attentionPulse = {
  backgroundColor: [
    'var(--color-accent-soft)',
    'color-mix(in srgb, var(--color-accent) 16%, transparent)',
    'var(--color-accent-soft)',
    'color-mix(in srgb, var(--color-accent) 16%, transparent)',
    'var(--color-accent-soft)',
  ] as string[],
  transition: { duration: duration.emphasis, ease: easing.standard },
};

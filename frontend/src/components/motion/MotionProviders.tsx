import React from 'react';
import { LazyMotion, MotionConfig, domAnimation } from 'motion/react';

/**
 * Global motion providers, wrapped once at the app root.
 *
 * - `LazyMotion features={domAnimation}` ships the small (~animation-only)
 *   feature bundle in the main chunk; `strict` forbids `motion.*` so every
 *   call site must use the tree-shakeable `m.*` component.
 * - `MotionConfig reducedMotion="user"` is the **only** place the motion
 *   library is told to honour the OS setting. The library ships
 *   `reducedMotion: "never"` (see `framer-motion/.../MotionConfigContext.mjs`),
 *   so without this line every `m.*` animation in the app plays at full speed
 *   under `prefers-reduced-motion: reduce`. Over 20 files animate; leaving it to
 *   each of them means one hand-rolled `reduceMotion ? …` branch and the rest
 *   silently ignoring the setting.
 *
 *   `"user"` disables the **positional** keys only — `width`, `height`,
 *   `top`/`left`/`right`/`bottom` and every transform prop
 *   (`motion-dom/.../keys-position.mjs`). Opacity and colour still cross-fade,
 *   which is the intended reading of "reduce": nothing travels, things may
 *   still appear. So call sites **must not** carry reduce branches of their own.
 *
 * The CSS half of this contract is one block in `index.css`
 * (`@media (prefers-reduced-motion: reduce)`); imperative JS motion that is
 * neither CSS nor a tween (Leaflet `flyTo`, the homepage prompt carousel) reads
 * the same setting through the library's `useReducedMotion()` hook.
 *
 * The itinerary canvas upgrades to the heavier `domMax` bundle locally via
 * `CanvasMotion` (lazy chunk), so layout/reorder cost stays off the main path.
 */
export const MotionProviders: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <MotionConfig reducedMotion="user">
    <LazyMotion features={domAnimation} strict>
      {children}
    </LazyMotion>
  </MotionConfig>
);

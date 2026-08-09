import React from 'react';
import { LazyMotion } from 'motion/react';

/**
 * Local feature boundary for the Living Itinerary canvas.
 *
 * The itinerary canvas needs layout / reorder animation (`domMax`), which is
 * ~2× the size of the global `domAnimation` bundle. Loading it as an async
 * feature keeps `domMax` in its own chunk, off the main bundle. Nested inside
 * the global `LazyMotion`, this only adds the extra features for its subtree.
 *
 * Wrap the canvas subtree with this; `m.*` components inside can then use
 * `layout`, `layoutId`, and `Reorder`.
 */
const loadDomMax = () => import('../../lib/motion-features').then((mod) => mod.default);

export const CanvasMotion: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <LazyMotion features={loadDomMax}>{children}</LazyMotion>
);

/**
 * `domMax` feature bundle, isolated in its own module so it is only ever pulled
 * in through a dynamic import — keeping layout / drag / reorder features out of
 * the main bundle. The global provider ships `domAnimation` (small); only the
 * itinerary-canvas subtree upgrades to `domMax` via `CanvasMotion`.
 *
 * Packaging strategy: one feature bundle per tier, never both in the main chunk.
 */
import { domMax } from 'motion/react';

export default domMax;

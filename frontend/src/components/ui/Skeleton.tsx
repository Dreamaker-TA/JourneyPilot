import React from 'react';
import { cn } from '../../lib/utils';

/**
 * Loading skeleton primitive.
 *
 * One shape for every loading placeholder — pages must not hand-roll
 * `<div className="h-X animate-pulse rounded bg-surface" />`.
 *
 * Size is supplied by the caller through `className` (height/width utilities),
 * matching the existing usage; `radius` and `tone` cover the two surface bases
 * (`surface` / `panel`) skeletons are drawn on across the app.
 */
type SkeletonRadius = 'label' | 'card' | 'full' | 'none';
type SkeletonTone = 'surface' | 'panel';

export interface SkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  radius?: SkeletonRadius;
  tone?: SkeletonTone;
}

// 形状分级的三个非零档。骨架屏占的是**将要出现的那个东西**的位，
// 所以这里的档名必须是**真身的角色名**，不能是 sm/md/lg 这类尺寸名 —— 真身只有两个角色，
// 尺寸名一多，两个不同的档在真身上就落到同一档，骨架占的位和真身对不上。
const radiusClass: Record<SkeletonRadius, string> = {
  none: '',
  label: 'rounded-label',
  card: 'rounded-card',
  full: 'rounded-full',
};

const toneClass: Record<SkeletonTone, string> = {
  surface: 'bg-surface',
  panel: 'bg-panel',
};

export const Skeleton: React.FC<SkeletonProps> = ({
  radius = 'card',
  tone = 'surface',
  className,
  ...props
}) => (
  <div
    className={cn('animate-pulse', radiusClass[radius], toneClass[tone], className)}
    {...props}
  />
);

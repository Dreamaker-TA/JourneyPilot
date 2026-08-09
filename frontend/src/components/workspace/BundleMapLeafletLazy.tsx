import React, { lazy, Suspense } from 'react';
import { LazyViewBoundary } from '../ui/LazyViewBoundary';
import type { MapProjection } from '../../types/delivery';
import type { BundleMapPlace } from './BundleMapLeaflet';

interface BundleMapLeafletLazyProps {
  places: BundleMapPlace[];
  routes: MapProjection['content']['routes'];
  selectedEntityId: string | null;
  onSelectEntity: (entityId: string) => void;
  linkedEntityId?: string | null;
  onLinkEntity?: (entityId: string | null) => void;
}

const BundleMapLeafletImpl = lazy(() => import('./BundleMapLeaflet').then((module) => ({
  default: module.BundleMapLeaflet,
})));

export const BundleMapLeafletLazy: React.FC<BundleMapLeafletLazyProps> = (props) => (
  <LazyViewBoundary>
    <Suspense fallback={(
      <div data-testid="bundle-map-loading" className="flex h-full items-center justify-center bg-surface text-xs text-ink-muted">
        地图加载中…
      </div>
    )}>
      <BundleMapLeafletImpl {...props} />
    </Suspense>
  </LazyViewBoundary>
);

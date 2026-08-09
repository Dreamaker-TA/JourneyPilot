import { useSyncExternalStore } from 'react';

/**
 * `<lg`（Tailwind lg=1024px 之下）即移动端工作台形态：
 * 画布不走桌面右侧停靠，而是经 bottom sheet 呈现。此断点是移动画布的唯一判定源，
 * MessageBubble 的「在画布查看行程」与 TripWorkspaceShell 的 Sheet 挂载共用它，
 * 保证触点与容器永远指向同一形态。
 */
const MOBILE_QUERY = '(max-width: 1023.98px)';

function subscribe(callback: () => void): () => void {
  const mql = window.matchMedia(MOBILE_QUERY);
  mql.addEventListener('change', callback);
  return () => mql.removeEventListener('change', callback);
}

function getSnapshot(): boolean {
  return window.matchMedia(MOBILE_QUERY).matches;
}

/** 当前视口是否处于移动端工作台形态（`<lg`）；随视口变化实时更新。 */
export function useIsMobileViewport(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot);
}

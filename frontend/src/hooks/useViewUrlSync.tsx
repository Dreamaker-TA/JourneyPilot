import { useEffect, useRef } from 'react';
import { useApp, isActiveView } from '../context/AppContext';

/**
 * 将 `activeView` 与 `?view=` 查询参数双向同步（初始视图由 AppContext 从 URL 读取）。
 *
 * 导航语法：视图切换用 `pushState` 进历史栈，
 * 浏览器后退/前进逐级归位；`popstate` 把 URL 上的视图 dispatch 回状态。
 *
 * 三处边界：
 * 1. 初始加载不 push——首个历史条目已代表初始视图；用 `replaceState` 把
 *    当前视图写进 `?view=`（既有落位行为），保证 back 能回到它。
 * 2. 同视图重复切换不产生历史条目——URL 已是目标视图时直接跳过。
 * 3. popstate 恢复视图不反向 push——用 `fromPopRef` 标记该次状态变化来自
 *    浏览器导航，同步 effect 见到标记即跳过（防循环）。
 *
 * 其余 query 参数（`?view=` 之外）在两个方向都原样保留。
 */
export function ViewUrlSync() {
  const { state, dispatch } = useApp();
  const activeView = state.activeView;
  // 该次 activeView 变化是否由 popstate 触发 —— 是则同步 effect 不反向 push。
  const fromPopRef = useRef(false);
  // 初始加载只 replace（不进栈），之后的切换才 push。
  const initializedRef = useRef(false);

  // 状态 → URL。
  useEffect(() => {
    if (fromPopRef.current) {
      // 本次 activeView 变化来自浏览器后退/前进：URL 已由浏览器还原，勿再写栈。
      fromPopRef.current = false;
      return;
    }

    const url = new URL(window.location.href);
    if (url.searchParams.get('view') === activeView) {
      // URL 已是目标视图（含初始落位 / 同视图重复点击）——不产生重复历史条目。
      initializedRef.current = true;
      return;
    }
    url.searchParams.set('view', activeView);
    const next = `${url.pathname}${url.search}${url.hash}`;

    if (!initializedRef.current) {
      // 初始加载：把当前视图写进 URL 但不进栈。
      initializedRef.current = true;
      window.history.replaceState(null, '', next);
    } else {
      // 用户切换视图：进历史栈，back/forward 可用。
      window.history.pushState(null, '', next);
    }
  }, [activeView]);

  // URL → 状态（浏览器后退/前进）。
  useEffect(() => {
    const handlePopState = () => {
      const v = new URLSearchParams(window.location.search).get('view');
      const nextView = isActiveView(v) ? v : 'chat';
      // 标记来源，让状态 → URL 的 effect 跳过反向 push（防循环）。
      fromPopRef.current = true;
      dispatch({ type: 'SET_ACTIVE_VIEW', payload: nextView });
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, [dispatch]);

  return null;
}

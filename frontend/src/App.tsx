import React, { Suspense, lazy } from 'react';
import { AnimatePresence, m } from 'motion/react';
import { AppProvider, useApp } from './context/AppContext';
import { MotionProviders } from './components/motion/MotionProviders';
import { MainLayout } from './layouts/MainLayout';
import { ChatView } from './components/chat/ChatView';
import { PageSkeleton } from './components/ui/PageSkeleton';
import { LazyViewBoundary } from './components/ui/LazyViewBoundary';
import { useSessionManager } from './hooks/useSessionManager';
import { ViewUrlSync } from './hooks/useViewUrlSync';
import { DeliveryEventRecovery } from './hooks/useDeliveryEventRecovery';
import { duration, easing } from './lib/motion';

/**
 * 非 chat 三视图走 React.lazy 分包——各页
 * 编译进独立 async chunk，主 bundle 不再打包它们的子树（及其独占依赖）。chat 视图
 * 是默认首屏，保持同步 import，首次进入零额外网络往返。
 *
 * 三页均为具名导出（`export const XxxPage`），React.lazy 只认 default 导出，故用
 * `.then` 把具名导出转成 default 形状。
 */
const KnowledgeBasePage = lazy(() =>
  import('./components/pages/KnowledgeBasePage').then((m) => ({ default: m.KnowledgeBasePage }))
);
const PresetLibraryPage = lazy(() =>
  import('./components/pages/PresetLibraryPage').then((m) => ({ default: m.PresetLibraryPage }))
);
const UserPreferencesPage = lazy(() =>
  import('./components/pages/UserPreferencesPage').then((m) => ({ default: m.UserPreferencesPage }))
);

const ViewContent: React.FC<{ view: string }> = ({ view }) => {
  switch (view) {
    case 'chat':
      return <ChatView />;
    case 'knowledge-base':
      return <KnowledgeBasePage />;
    case 'presets':
      return <PresetLibraryPage />;
    case 'user-preferences':
      return <UserPreferencesPage />;
    default:
      return <ChatView />;
  }
};

/**
 * 视图切换过渡：AnimatePresence `mode="wait"`
 * 以 activeView 为 key。`mode="wait"` 让旧视图退场后新视图才挂载，各页挂载时的数据
 * 加载行为照常触发；只 animate opacity，不碰布局属性。
 *
 * **这一层只管淡入淡出，4px 上移交给各面自己的入场编排**：这里**不要**再做 `y: 4 → 0`。
 * 各面有自己的 `staggerItem`（同样是位移），两层同时位移会让同一批帧被推两次 —— 整块先
 * 上移 4px，里面每一行再各上移 8px，读起来是一次「抖一下再排队」。位移只能有一个作者。
 * 骨架与真实内容仍共用这一次淡入（`PageSkeleton` 的注释依赖这一点），不出现双闪。
 *
 * 懒加载分包嵌套：`LazyViewBoundary` → `Suspense` 都放在入场
 * `m.div` **内部**。冷 chunk 首次进入时，Suspense 的 `PageSkeleton` fallback 与
 * 之后加载好的真实内容共用同一次入场过渡（fallback 随 m.div 一起淡入上移，chunk
 * 到位后就地替换为真实内容）——不会出现「过渡完成后再闪一次白」的双闪。热 chunk
 * （二次进入）Suspense 不挂起，直接渲染真实内容，与非懒视图体感一致。
 * chunk 加载失败时 LazyViewBoundary 兜底为一句文案 + 重新加载，不白屏。
 */
const ViewRouter: React.FC = () => {
  const { state } = useApp();
  const view = state.activeView;
  const boundaryResetKey = view === 'chat' ? `${view}:${state.conversationEpoch}` : view;

  return (
    <AnimatePresence mode="wait" initial={false}>
      <m.div
        key={view}
        className="h-full min-h-0"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1, transition: { duration: duration.base, ease: easing.decelerate } }}
        exit={{ opacity: 0, transition: { duration: duration.fast, ease: easing.accelerate } }}
      >
        <LazyViewBoundary resetKey={boundaryResetKey}>
          <Suspense fallback={<PageSkeleton />}>
            <ViewContent view={view} />
          </Suspense>
        </LazyViewBoundary>
      </m.div>
    </AnimatePresence>
  );
};

const App: React.FC = () => {
  return (
    <AppProvider>
      <MotionProviders>
        <SessionBootstrap />
        <DeliveryEventRecovery />
        <ViewUrlSync />
        <MainLayout>
          <ViewRouter />
        </MainLayout>
      </MotionProviders>
    </AppProvider>
  );
};

const SessionBootstrap: React.FC = () => {
  const { initializeSessions } = useSessionManager();

  React.useEffect(() => {
    void initializeSessions();
  }, [initializeSessions]);

  return null;
};

export default App;

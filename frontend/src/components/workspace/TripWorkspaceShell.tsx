import React from 'react';
import { m } from 'motion/react';
import { Map as MapIcon } from 'lucide-react';
import { useApp } from '../../context/AppContext';
import { useIsMobileViewport } from '../../hooks/useMediaQuery';
import { useCurrentBundleWeatherRefresh } from '../../hooks/useCurrentBundleWeatherRefresh';
import { cn } from '../../lib/utils';
import { attentionPulse, emphasisEnter } from '../../lib/motion';
import { api } from '../../lib/api';
import { isPublicDeliveryBundle } from '../../types/delivery';
import { Sheet } from '../ui/Sheet';
import { ConversationThread } from '../chat/ConversationThread';
import { DeliveryWorkspace } from './DeliveryWorkspace';
import { Neatline } from '../ui/Neatline';

export const TripWorkspaceShell: React.FC = () => {
  const { state, dispatch, currentStateRef } = useApp();
  const isMobile = useIsMobileViewport();

  const retryCurrentBundle = React.useCallback(async () => {
    const runId = state.currentTripRunId;
    if (!runId) return;
    dispatch({
      type: 'SET_DELIVERY_BUNDLE_LOAD_STATE',
      payload: { status: 'loading', message: null },
    });
    try {
      const bundle = await api.getCurrentDeliveryBundle(runId, state.userId, state.currentSessionId);
      if (!isPublicDeliveryBundle(bundle) || bundle.manifest.run_id !== runId) {
        throw new Error('delivery bundle contract mismatch');
      }
      if (currentStateRef.current.currentTripRunId !== runId) return;
      dispatch({ type: 'CONFIRM_DELIVERY_BUNDLE', payload: bundle });
    } catch {
      if (currentStateRef.current.currentTripRunId !== runId) return;
      dispatch({
        type: 'SET_DELIVERY_BUNDLE_LOAD_STATE',
        payload: {
          status: 'error',
          message: '暂时无法加载这趟旅行的正式结果，请稍后重试。',
        },
      });
    }
  }, [currentStateRef, dispatch, state.currentSessionId, state.currentTripRunId, state.userId]);

  const hasCanvasWork = state.deliveryBundle !== null;
  const showCanvas = state.canvasOpen && hasCanvasWork;
  const fullscreen = showCanvas && state.canvasFullscreen;

  /*
   * 表现轴的第一个瞬间：**行程第一次显形**（Motion「Two axes」）。
   *
   * `emphasisEnter`（`--dur-emphasis` + 轻微 spring 过冲）在这里落地。两个被批准过冲的
   * 瞬间里，这是最重要的一个：整个产品就是为了产出它。它同时**就是**「方案就绪」——
   * 行程显形即方案就绪，不存在方案好了而行程还没出来的一刻，所以合同里没有第三个瞬间。
   *
   * 只在**第一次**播。此后画布会因为 lg 断点、全屏切换、Bundle 更新反复挂载卸载，
   * 每次都过冲一遍就变成了装饰（「每屏最多一个表现瞬间」）。ref 挂在本组件实例上，
   * 移动端 Sheet 与桌面停靠共用同一个开关。
   */
  const canvasRevealedRef = React.useRef(false);
  React.useEffect(() => {
    if (showCanvas) canvasRevealedRef.current = true;
  }, [showCanvas]);
  const firstCanvasReveal = showCanvas && !canvasRevealedRef.current;
  useCurrentBundleWeatherRefresh(showCanvas && state.deliverableView === 'interactive_itinerary');

  return (
    <div className="flex h-full flex-col overflow-hidden bg-bg">
      {(state.deliveryBundleLoadState.status === 'error' || state.deliveryBundleLoadState.status === 'loading') && (
        <div className="flex-none px-3 pt-3 sm:px-4">
          <div
            role="alert"
            className="mx-auto flex max-w-3xl items-center justify-between gap-3 rounded-card border border-error/25 bg-panel px-4 py-3 text-sm text-ink shadow-sm"
          >
            <span className="min-w-0 break-words">
              {state.deliveryBundleLoadState.status === 'loading'
                ? '正在重新加载这趟旅行…'
                : state.deliveryBundleLoadState.message}
            </span>
            <button
              type="button"
              onClick={() => void retryCurrentBundle()}
              disabled={state.deliveryBundleLoadState.status === 'loading'}
              className="shrink-0 rounded-card border border-stroke bg-surface px-3 py-1.5 text-xs font-semibold text-ink transition-colors hover:bg-accent-soft disabled:cursor-wait disabled:opacity-60"
            >
              重试
            </button>
          </div>
        </div>
      )}

      {/**
       * **这条天头是画布那两块面的天头，只在有面的时候给**。
       *
       * `p-3 sm:p-4` 此前无条件加在这一层上，于是无画布形态（首页空态、全宽对话）也被
       * 往里缩 16px —— 而 §Layout 给无画布形态的口径是「透明、全宽、**直接坐在页面背景上**，
       * 没有卡框没有底色」。对话看不出来（正文自己带 `px-4 py-6`），**首页看得一清二楚**：
       * 图纸家具画到容器边缘为止，于是窗口四边各留一条 16px 没有网格的空纸，上下两条正对着
       * 用户的视线 —— 用户报的就是这个「上下都有条」。
       *
       * 这也是「一个角色两套值」在间距轴上的样子：**窗口边到正文的距离被写了两遍**（外壳的
       * 天头 + 正文自己的内边距），而其中一遍只在画好家具的那一屏才现形。天头归有面的那一
       * 档，正文的内缩归正文自己。`gap-3` 同理 —— 它分的是两块面，一块面时没有缝要分。
       */}
      <div
        data-testid="trip-workspace-layout"
        className={cn(
          'min-h-0 flex-1',
          showCanvas && 'gap-3 p-3 sm:p-4',
          showCanvas && !fullscreen
            ? 'flex lg:grid lg:grid-cols-[minmax(0,4.5fr)_minmax(0,5.5fr)]'
            : 'flex'
        )}
      >
        {/* 对话线程：`<lg` 常驻（画布走 Sheet，不再顶掉对话）；桌面停靠/全屏行为不变。
            有可展示行程：卡片分层（border+bg-panel+shadow）与分栏；否则透明全宽、直接坐在页面背景上。
            relative 作为画布悬浮 pill 的定位父容器——pill 悬浮在会话区右上角。 */}
        {/* 外壳是**图纸**这一档形状：半径 0，边界由 neatline 与四角登记刻线给出，不由圆角
            给出。**不要**给它任何圆角：合同写的卡面上限是 12px，而它里面那张卡本身就是
            20px —— 外壳一带半径就与内容同档，两级读不出层级，只读出一团圆。 */}
        <div
          data-testid="workspace-conversation"
          className={cn(
            'relative flex min-h-0 min-w-0 flex-col overflow-hidden',
            showCanvas
              ? fullscreen
                ? 'flex-1 bg-panel shadow-sm lg:hidden'
                : 'flex-1 bg-panel shadow-sm'
              : 'w-full flex-1'
          )}
        >
          {showCanvas && <Neatline />}
          <CanvasPill />
          <ConversationThread />
        </div>

        {/* 桌面停靠画布只在 lg+ 真实挂载——移动端画布走 Sheet，不留隐藏双胞胎。 */}
        {showCanvas && !isMobile && (
          <m.div
            data-testid="workspace-canvas"
            className={cn('min-h-0 min-w-0', fullscreen && 'flex-1')}
            variants={emphasisEnter}
            initial={firstCanvasReveal ? 'hidden' : false}
            animate="visible"
          >
            <DeliveryWorkspace bundle={state.deliveryBundle!} />
          </m.div>
        )}
      </div>

      {/* 移动端画布：`<lg` 经 bottom sheet 呈现全部四 tab。 */}
      {isMobile && hasCanvasWork && (
        <Sheet
          open={state.mobileCanvasOpen}
          onClose={() => dispatch({ type: 'SET_MOBILE_CANVAS_OPEN', payload: false })}
          testId="canvas-sheet"
        >
          <DeliveryWorkspace bundle={state.deliveryBundle!} variant="sheet" />
        </Sheet>
      )}
    </div>
  );
};

/**
 * 会话区悬浮「画布」pill（§5.1）：`<lg` 移动端唯一常驻的画布入口，含当前 tab 名。
 * 绝对定位在会话区右上角，悬浮于消息滚动内容之上、不遮挡底部输入区；≥44px 热区。
 * 流式产出首个面板数据的那一刻做一次 attentionPulse——落点提示，不循环。
 */
const CanvasPill: React.FC = () => {
  const { state, dispatch } = useApp();
  const hasCanvasWork = state.deliveryBundle !== null;
  const isStreaming = state.isStreaming;

  const [pulse, setPulse] = React.useState(false);
  const prevHasCanvasWork = React.useRef(hasCanvasWork);
  React.useEffect(() => {
    if (!prevHasCanvasWork.current && hasCanvasWork && isStreaming) setPulse(true);
    prevHasCanvasWork.current = hasCanvasWork;
  }, [hasCanvasWork, isStreaming]);

  if (!hasCanvasWork) return null;

  return (
    <m.button
      type="button"
      data-testid="canvas-pill"
      onClick={() => dispatch({ type: 'SET_MOBILE_CANVAS_OPEN', payload: true })}
      animate={pulse ? attentionPulse : undefined}
      className={cn(
        'absolute right-3 top-3 z-20 inline-flex items-center gap-1.5 rounded-full',
        'border border-accent/25 bg-[var(--color-accent-soft)] px-3 py-1.5 text-xs font-semibold text-accent shadow-sm lg:hidden'
      )}
    >
      <MapIcon size={12} />
      <span className="max-w-[9em] truncate">查看行程</span>
    </m.button>
  );
};

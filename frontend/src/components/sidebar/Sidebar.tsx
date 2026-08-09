import React, { useState } from 'react';
import { useApp, type ActiveView } from '../../context/AppContext';
import { SidebarSearch } from './SidebarSearch';
import { ConversationList } from './ConversationList';
import { SidebarNav } from './SidebarNav';
import { Plus, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { Button } from '../ui/Button';
import { BrandMark, BrandWordmark } from '../ui/BrandMark';
import { Tooltip } from '../ui/Tooltip';
import { cn } from '../../lib/utils';
import { useSessionManager } from '../../hooks/useSessionManager';
import { RAIL_GUTTER, RAIL_LABEL, RAIL_SLOT, RAIL_SLOT_INDENT } from './rail';

export const Sidebar: React.FC = () => {
  const { state, dispatch, activeStreamAbortRef } = useApp();
  const { setLastSession } = useSessionManager();
  const [searchQuery, setSearchQuery] = useState('');
  const collapsed = state.sidebarCollapsed;
  const searchInputRef = React.useRef<HTMLInputElement>(null);

  const handleNewChat = () => {
    if (activeStreamAbortRef.current) {
      activeStreamAbortRef.current.abort();
      activeStreamAbortRef.current = null;
    }
    dispatch({ type: 'CLEAR_CHAT' });
    setLastSession(null);
    dispatch({ type: 'SET_ACTIVE_VIEW', payload: 'chat' });
  };

  /** 折叠开关的名字 = 它此刻会做的那件事。tooltip 与 `aria-label` 消费同一份值。 */
  const sidebarToggleLabel = collapsed ? '展开侧栏' : '收起侧栏';

  const handleNavChange = (view: ActiveView) => {
    dispatch({ type: 'SET_ACTIVE_VIEW', payload: view });
  };

  // 折叠态搜索钮 = 展开侧栏 + 聚焦搜索框。输入框始终在 DOM 内（折叠仅是 CSS
  // grid/opacity），下一帧即可功能性聚焦（允许的功能性自动聚焦，非辅助层）。
  const handleExpandAndFocusSearch = () => {
    dispatch({ type: 'SET_SIDEBAR_COLLAPSED', payload: false });
    requestAnimationFrame(() => {
      searchInputRef.current?.focus();
    });
  };

  return (
    <div className="flex h-full w-full flex-col">
      {/**
       * 头部：展开态是「品牌标记坐在刻度栏上 + 字标 + 右缘折叠开关」；折叠态那条槽
       * 让给折叠开关本身 —— 68px 宽的轨道里只容得下一条槽，而能把轨道叫回来的控件
       * 比一枚标记更该占着它。品牌块折叠时**整块退出布局**（不是淡出）：两个控件争同一条
       * 槽时，淡出会让先到的那个把后到的推走。
       *
       * 折叠开关必须是侧栏里 DOM 顺序上的第一枚 `<button>` —— 折叠逻辑
       * 按 `[data-testid="app-sidebar"] button` 的 first() 取。
       */}
      <div className={cn('relative flex items-center pb-2 pt-3', RAIL_GUTTER)}>
        {!collapsed && (
          <div className={cn('flex min-w-0 flex-1 items-center overflow-hidden', RAIL_SLOT_INDENT)}>
            <span className={RAIL_SLOT}>
              <BrandMark size={22} />
            </span>
            {/* 产品名是字标本身，不是一句系统无衬线。字标紧跟槽的右缘 = 54px。 */}
            <BrandWordmark height={13} className="shrink-0" />
          </div>
        )}
        {/**
         * 这枚钮的名字与那句微释义是**同一份值**：一个局部常量、两处消费。
         * 名字写在控件自己身上（`aria-label`），tooltip 不负责接名字 —— 理由在
         * `ui/Tooltip` 的头注释。
         *
         * 而且它**随状态走**：写死「展开侧栏」的话，侧栏已经展开、这枚钮的作用是收起时，
         * 屏幕上那句话说的是反的。一个只有字形的钮，名字说反等于点错。
         */}
        <Tooltip content={sidebarToggleLabel} position="right" disabled={!collapsed}>
          <Button
            variant="icon"
            aria-label={sidebarToggleLabel}
            className={cn('!h-10 !w-10 shrink-0', collapsed && 'ml-1.5')}
            onClick={() => dispatch({ type: 'TOGGLE_SIDEBAR' })}
          >
            {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
          </Button>
        </Tooltip>
      </div>

      <div className="mb-3 border-t border-stroke/60" />

      {/**
       * 新建行程 —— 轨道的主动作。
       *
* `w-full` 两态都在，宽度跟着轨道走（跟随不是过渡：没有 `transition` 挂在
        * width 上，所以「唯一获准的布局过渡」仍然只有轨道自己那一条）。
       * 加号坐在刻度栏的槽里、`justify-start`，所以折叠态它落在轨道中心线上、展开态
       * 落在左脊上 —— **两态同一个横坐标**。标签绝对定位，推不动加号。
       *
* 半径是卡档（8px），**不是** `!rounded-full`：「A container is never a circle;
        * a mark always is」，而按钮是容器。
       */}
      <div className={cn('pb-3', RAIL_GUTTER)}>
        <Tooltip content="新建行程" position="right" disabled={!collapsed} className="w-full">
          <Button
            variant="primary"
            size="md"
            onClick={handleNewChat}
            /**
             * 三处 `!`，三处都是同一个原因：**`cn` 是纯 clsx，没有 tailwind-merge**，
             * 所以 `Button` 自己那份 `justify-center` / `px-4` / `gap-2` 不会被这里的
             * 覆盖类「合并掉」，两条都进 class 串，谁赢由 Tailwind 的**输出顺序**决定。
             * 而 Tailwind 把 `justify-start` 排在 `justify-center` **之前** —— 于是不带
             * `!` 的 `justify-start` 静默失效，实测加号中心停在 33.0（差 1px，正好是
             * 40px 的槽挤进 38px 内容盒后向两边各溢出的那 1px）。
             * `!pl-1.5 !pr-2` 而不是 `!px-0`：`px` 与 `pl` 同理，两侧各写一条就不依赖顺序。
             */
            /* `touch-row`：同 `SidebarNav` 那三行 —— 这枚钮裁自己的 overflow（标签
               要被收掉），所以按元素选的那条命中区外扩落不到它身上，44px 地板只能落在盒子
               本身，且只在 coarse 指针下。桌面仍是 h-10。 */
            className="touch-row relative flex h-10 w-full items-center overflow-hidden !justify-start !gap-0 !pl-1.5 !pr-2"
          >
            <span className={RAIL_SLOT}>
              <Plus size={18} />
            </span>
            <span className={cn(RAIL_LABEL, 'text-sm', collapsed ? 'opacity-0' : 'opacity-100')}>
              新建行程
            </span>
          </Button>
        </Tooltip>
      </div>

      {/* 搜索：两态同一条槽 —— 展开是一条输入场，折叠是槽里的一枚放大镜。 */}
      <div className={cn('pb-3', RAIL_GUTTER)}>
        <SidebarSearch
          value={searchQuery}
          onChange={setSearchQuery}
          collapsed={collapsed}
          inputRef={searchInputRef}
          onExpandAndFocus={handleExpandAndFocusSearch}
        />
      </div>

      {/**
       * 会话列表。折叠态**不渲染** —— **不要**改成 `opacity-0 pointer-events-none`：那样它
       * 看不见却照常占着 `flex-1` 的滚动内容与全部 DOM，一份看不见的列表还在跟着滚。
       * `flex-1` 挂在这个占位容器上，底部导航的位置两态不变。
       */}
      <div data-testid="sidebar-conversations" className={cn('min-h-0 flex-1 overflow-y-auto', RAIL_GUTTER)}>
        {!collapsed && <ConversationList searchQuery={searchQuery} />}
      </div>

      <SidebarNav activeView={state.activeView} onChange={handleNavChange} collapsed={collapsed} />
    </div>
  );
};

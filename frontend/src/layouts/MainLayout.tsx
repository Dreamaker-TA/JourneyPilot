import React from 'react';
import { PanelLeftOpen } from 'lucide-react';
import { cn } from '../lib/utils';
import { useApp } from '../context/AppContext';
import { Sidebar } from '../components/sidebar/Sidebar';

interface MainLayoutProps {
  children: React.ReactNode;
}

const LAYOUT_EASE = 'ease-standard';
/**
 * 轨道自己那一条 `width` 是全站唯一获准的布局过渡（合同 §Motion 有明文口子）：折叠**就是**
 * 宽度变化，主区必须跟着重排。轨道里面的东西一律只动 transform/opacity。
 *
 * `transform` 是给移动端的：小屏上侧栏是覆盖层，靠 `-translate-x-full` 推出屏外。
 * 这里**不列** `opacity` —— 它从不变，列上去就是一条死过渡。
 */
const SIDEBAR_TRANSITION = 'transition-[transform,width] duration-slow ' + LAYOUT_EASE;

/** 应用外壳：可折叠侧边栏 + 单一大主区（Claude.ai 式）。主区内容由 ViewRouter 决定。 */
export const MainLayout: React.FC<MainLayoutProps> = ({ children }) => {
  const { state, dispatch } = useApp();
  const { sidebarCollapsed } = state;

  return (
    <div className="paper-grain flex h-screen w-screen overflow-hidden bg-bg">
      {/* Mobile sidebar overlay */}
      {!sidebarCollapsed && (
        <div
          data-testid="sidebar-scrim"
          className="fixed inset-0 z-20 bg-ink/25 backdrop-blur-sm transition-opacity duration-base lg:hidden"
          onClick={() => dispatch({ type: 'TOGGLE_SIDEBAR' })}
        />
      )}

      {/**
       * 小屏上唯一的导航入口。
       *
       * 低于 `lg` 时侧栏是**覆盖层**，折叠态被 `-translate-x-full` 整个推到屏外（实测
       * `x = -68`）——连同它自己那枚开关一起。而初始态就是折叠（`window.innerWidth < 1024`），
       * 于是小屏一打开产品，**全部导航不可达**：没有任何一个控件能把侧栏叫回来。
       *
       * 所以这枚按钮只在「侧栏此刻够不着」的那一种情况下出现：`lg` 及以上侧栏永远在场
       * （折叠也留 68px 的轨），展开时侧栏自己那枚开关就在眼前。两个条件都写出来，
       * 而不是无条件挂一枚——多一个常驻悬浮控件是 §Anti-Slop 要躲的东西。
       *
       * 44px 见方，满足 §Component Rules 的移动端触达下限。
       */}
      {sidebarCollapsed && (
        <button
          type="button"
          data-testid="sidebar-open"
          aria-label="打开导航"
          onClick={() => dispatch({ type: 'TOGGLE_SIDEBAR' })}
          className={cn(
            'fixed left-3 top-3 z-30 flex h-11 w-11 items-center justify-center lg:hidden',
            'rounded-card border border-stroke bg-panel text-ink-secondary shadow-sm',
            'transition-colors duration-base hover:text-ink',
            '',
          )}
        >
          <PanelLeftOpen size={18} aria-hidden />
        </button>
      )}

      {/* Sidebar */}
      {/**
       * 轨道的宽度算式必须是**准的**。
       *
       * 折叠态 68px 里要放一条 40px 的字形槽居中（见 `sidebar/rail.ts`）：14 + 40 + 14。
       * 所以右侧那条分隔线由 `box-shadow: 1px 0 0` 画，**不用 `border-r`**：边框在
       * `box-sizing: border-box` 下吃掉 1px 宽，可见面板只剩 67px、视觉中心变成 33.5 而不是
       * 34，槽里每一枚字形都偏右半像素，而 `14` / `11` 这两个数就成了没来由的魔数。阴影不占
       * 盒模型，68px 全归内容，算式对得上，三处「折叠正好 68px」的判据也都成立。
       *
       * 两层阴影写在一起（分隔线 + 那道极淡的投影），且不参与任何过渡
       * （`transition-[transform,width]` 里没有 box-shadow —— §Motion 明禁动画 box-shadow）。
       */}
      <aside
        data-testid="app-sidebar"
        className={cn(
          'absolute left-0 top-0 h-full flex-shrink-0 lg:relative',
          SIDEBAR_TRANSITION,
          'z-30 overflow-hidden bg-panel lg:z-10',
          'shadow-[1px_0_0_rgb(var(--color-stroke-rgb)),4px_0_24px_rgba(32,33,36,0.04)]',
          sidebarCollapsed ? '-translate-x-full lg:translate-x-0 !w-[68px]' : 'translate-x-0 !w-[280px]'
        )}
      >
        <Sidebar />
      </aside>

      {/* Main workspace */}
      <main
        // 这里**不挂过渡**：`flex-1` 是常量、宽度由 flex 算、opacity 从不变，
        // `transition-[flex-grow,flex-shrink,opacity,width]` 那一条一个属性都动不到。
        //
        // `pt-14 lg:pt-0`：上面那枚「打开导航」是 `fixed left-3 top-3 h-11 w-11`，占住
        // (12,12)–(56,56) 而**完全不参与布局**。小屏上任何顶到边的内容都会从它下面穿过去
        // （首页 H1 的第一行就正压在按钮底下，被挡掉约半行）。空间由外壳一处预留
        // （56px = 12 + 44），**不由每一屏各自记得让开** —— 这枚按钮之上会经过的屏不止一个，
        // 让各屏各写一份，同一件事就多了一个定义处。
        //
        // **无条件预留，不跟着 `sidebarCollapsed` 变**：跟着状态变会让内容在开合侧栏时
        // 跳 56px。小屏侧栏是覆盖层，展开时这 56px 本来就在遮罩后面，什么都没损失。
        className="relative z-0 flex h-full min-w-0 flex-col overflow-hidden flex-1 pt-14 lg:pt-0"
      >
        {children}
      </main>
    </div>
  );
};

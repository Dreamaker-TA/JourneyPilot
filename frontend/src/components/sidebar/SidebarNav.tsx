import React from 'react';
import { cn } from '../../lib/utils';
import type { ActiveView } from '../../context/AppContext';
import { Tooltip } from '../ui/Tooltip';
import { BookOpen, Compass, Sliders } from 'lucide-react';
import { RAIL_GUTTER, RAIL_LABEL, RAIL_SLOT, RAIL_SLOT_INDENT } from './rail';

interface SidebarNavProps {
  activeView: ActiveView;
  onChange: (view: ActiveView) => void;
  collapsed?: boolean;
}

const navItems: Array<{
  view: ActiveView;
  icon: React.ReactNode;
  label: string;
}> = [
  // 这里**没有**「我的行程」：它列的是这个用户的每一趟旅行，而这条轨道
  // 上方那列会话记录列的是同一批东西 —— 同一份清单在一条轨道上出现两次，其中一份还要多
  // 走一次导航。一趟旅行的入口是那条记录行（`ConversationList` 的 `SessionRecord`，
  // trip 形状用的正是 `Route` 那一枚字形）。
  { view: 'knowledge-base', icon: <BookOpen size={18} />, label: '资料来源' },
  { view: 'presets', icon: <Compass size={18} />, label: '旅行风格' },
  // 字形要和名字说同一件事：**不用** `User`（它画的是「一个人」）—— 这一屏装的是出发地、
  // 六组偏好与记忆条目，是一组可调的刻度，不是一个人的头像。
  { view: 'user-preferences', icon: <Sliders size={18} />, label: '我的偏好' },
];

export const SidebarNav: React.FC<SidebarNavProps> = ({ activeView, onChange, collapsed }) => {
  return (
    <div className={cn('flex flex-col gap-0.5 border-t border-stroke/60 py-2', RAIL_GUTTER)}>
      {navItems.map(({ view, icon, label }) => (
        <Tooltip
          key={view}
          content={label}
          position="right"
          disabled={!collapsed}
          className="w-full"
        >
          <a
            href={`?view=${encodeURIComponent(view)}`}
            onClick={(e) => {
              e.preventDefault();
              onChange(view);
            }}
            className={cn(
              /* `touch-row`：这一行**必须**裁自己的 overflow（折叠时那枚绝对定位的
                 标签要被收掉），而一个裁 overflow 的控件长不出向外的命中区 —— 伪元素被宿主
                 自己裁掉。所以它的 44px 地板落在盒子本身上，只在 coarse 指针下生效：桌面
                 仍然是 40px 的槽。 */
              'touch-row relative flex h-10 w-full cursor-pointer items-center overflow-hidden rounded-card',
              'transition-[background-color,color] duration-base ease-standard',
              RAIL_SLOT_INDENT,
              activeView === view
                ? 'bg-accent/10 font-medium text-accent'
                : 'text-ink-secondary hover:bg-ink/[0.05] hover:text-ink'
            )}
          >
            <span className={RAIL_SLOT}>{icon}</span>
            {/* 标签绝对定位、不占流（见 `rail.ts`）：它推不动字形，所以两态下这枚字形的
                横坐标完全相同，动的只有它自己的不透明度。 */}
            <span className={cn(RAIL_LABEL, 'text-sm', collapsed ? 'opacity-0' : 'opacity-100')}>
              {label}
            </span>
          </a>
        </Tooltip>
      ))}
    </div>
  );
};

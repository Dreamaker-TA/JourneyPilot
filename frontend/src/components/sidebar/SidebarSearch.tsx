import React from 'react';
import { Search } from 'lucide-react';
import { cn } from '../../lib/utils';
import { Input } from '../ui/Input';
import { Tooltip } from '../ui/Tooltip';
import { RAIL_LABEL, RAIL_SLOT, RAIL_SLOT_INDENT } from './rail';

interface SidebarSearchProps {
  value: string;
  onChange: (value: string) => void;
  collapsed?: boolean;
  /** 折叠态搜索钮点击：展开侧栏并聚焦搜索框。 */
  onExpandAndFocus?: () => void;
  /** 展开态输入框 ref——供折叠钮展开后功能性聚焦（允许的功能性自动聚焦）。 */
  inputRef?: React.Ref<HTMLInputElement>;
}

/**
 * 搜索 —— 两态共用轨道的那一条字形槽。
 *
 * 它和导航行同一副身体：满宽、`pl-1.5`、字形坐在 40px 槽里 —— 折叠态一枚放大镜，展开态
 * 一条输入场。字形是 18px、中心落在 34，与轨道里其他每一枚一致；**不要**给折叠态另做一枚
 * `!h-10 !w-10` 的居中钮，那会让它的字形尺寸和中心线都和邻居差开。
 */
export const SidebarSearch: React.FC<SidebarSearchProps> = ({
  value,
  onChange,
  collapsed,
  onExpandAndFocus,
  inputRef,
}) => {
  if (collapsed) {
    // §4.5：折叠态搜索钮点击展开侧栏并聚焦搜索输入框 —— 它不能是一枚死按钮。
    return (
      <Tooltip content="搜索对话" position="right" className="w-full">
        <button
          type="button"
          data-testid="sidebar-search-collapsed"
          onClick={onExpandAndFocus}
          className={cn(
            'relative flex h-10 w-full items-center overflow-hidden rounded-card',
            'text-ink-secondary transition-[background-color,color] duration-base ease-standard',
            'hover:bg-ink/[0.05] hover:text-ink',
            RAIL_SLOT_INDENT
          )}
        >
          <span className={RAIL_SLOT}>
            <Search size={18} />
          </span>
          <span className={cn(RAIL_LABEL, 'text-sm opacity-0')}>搜索对话</span>
        </button>
      </Tooltip>
    );
  }

  return (
    <Input
      ref={inputRef}
      name="conversation-search"
      data-testid="sidebar-search-input"
      autoComplete="off"
      icon={<Search size={18} />}
      placeholder="搜索对话…"
      value={value}
      onChange={(event) => onChange(event.target.value)}
      // 图标坐在同一条槽里。这一层的盒子从距轨道边 8px 处起（组容器 `px-2`），所以
      // 槽的 14..54 在这里是 6..46：18px 的字形居中落在 17px 处（→ 轨道 25..43，
      // 中心 34），文字从 46px 起（→ 轨道 54px）。和导航行、新建行程同一条脊。
      iconClassName="left-[17px]"
      className="pl-[46px]"
    />
  );
};

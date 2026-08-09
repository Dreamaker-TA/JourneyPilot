import React from 'react';
import { AnimatePresence, m } from 'motion/react';
import { cn, groupByDate } from '../../lib/utils';
import { useApp } from '../../context/AppContext';
import { useSessionManager } from '../../hooks/useSessionManager';
import { MessageSquare, Pencil, Route, Trash2, X } from 'lucide-react';
import { parseSessionTitle } from '../../lib/sessionTitle';
import { describeRequestFailure } from '../../lib/requestFailureMessage';
import { Button } from '../ui/Button';
import { ConfirmAction } from '../ui/ConfirmAction';
import { duration, easing } from '../../lib/motion';
import type { ChatSession } from '../../types/chat';

interface ConversationListProps {
  searchQuery: string;
}

/**
 * 侧栏的一行 —— 一条**记录**，不是一句话。
 *
 * 侧栏是用来「扫」的：读者要在四行里找出自己那趟旅行，看的是目的地和日期。所以三个字段
 * 各有各的声部：目的地是加粗主词，出发地降为常规字重的前缀，日期自己成为一行等宽读数
 * （`8/5–8/8` 全是半角，`tabular-nums` 在这里是对的——它只用在纯数字列上，绝不用在中英
 * 混排的值上）。**不要**把整条标题按一句话排（统一字号统一字重）：那会让真正互不相同的
 * 那两个字段和不变的出发地、箭头一样重，尾部日期还常被挤到第二行行末。
 *
 * 无障碍名仍是**整条标题**：拆分是给眼睛的排版，读屏应该听到那一个规范字符串，而不是
 * 三段被空格拼起来的碎片。
 *
 * 字形坐在**轨道那条字形栏**上。
 *
 * 40px 见方的槽，和导航行、新建行程、搜索、品牌标记同一条（`sidebar/rail.ts`）：容器
 * `px-2` + 行内 `pl-1.5` = 14px，槽 14..54，字形居中落在 34；标题也从 54px 起，于是整条
 * 轨道从上到下只有**一条**文字左缘。这几个数必须和 `rail.ts` 对齐 —— 会话行自己另取一套
 * （比如 `px-2.5` + 14px 图标，中心落在 21），展开态的轨道上就会出现两条左缘。
 *
 * 字形本身仍是 14px（它是**内容**里的一枚标，不是一枚控件字形），槽把它居中。
 */
const RECORD_SLOT = 'flex w-10 shrink-0 justify-center pt-[3px]';

const SessionRecord: React.FC<{ title: string }> = ({ title }) => {
  const parsed = parseSessionTitle(title);
  if (parsed.kind === 'plain') {
    return (
      <>
        <span className={RECORD_SLOT}>
          <MessageSquare size={14} className="opacity-50" aria-hidden />
        </span>
        <span
          data-session-shape="plain"
          data-session-field="text"
          className="min-w-0 flex-1 break-words text-sm leading-[1.35] line-clamp-2"
        >
          {parsed.text}
        </span>
      </>
    );
  }
  return (
    <>
      <span className={RECORD_SLOT}>
        <Route size={14} className="opacity-50" aria-hidden />
      </span>
      <span data-session-shape="trip" className="min-w-0 flex-1" aria-label={title}>
        <span className="flex min-w-0 flex-wrap items-baseline gap-x-1 text-sm leading-[1.35]">
          {parsed.origin && (
            <>
              <span data-session-field="origin" className="min-w-0 break-words text-ink-secondary">
                {parsed.origin}
              </span>
              {/* 箭头走图纸墨蓝（chart）：它是路线符号，不是可点的东西。 */}
              <span className="shrink-0 text-chart" aria-hidden>→</span>
            </>
          )}
          <span data-session-field="destination" className="min-w-0 break-words font-semibold">
            {parsed.destination}
          </span>
        </span>
        {parsed.dates && (
          <span
            data-session-field="dates"
            className="mt-0.5 block font-mono text-[11px] tabular-nums text-ink-muted"
          >
            {parsed.dates}
          </span>
        )}
      </span>
    </>
  );
};

export const ConversationList: React.FC<ConversationListProps> = ({ searchQuery }) => {
  const { state, dispatch } = useApp();
  const { openSession, deleteSession, renameSession } = useSessionManager();
  const { sessions, currentSessionId } = state;

  // 行内重命名的编辑态：只允许一行同时进入编辑；draft 为空提交 = 取消。
  const [editingId, setEditingId] = React.useState<string | null>(null);
  const [draft, setDraft] = React.useState('');
  const [renameError, setRenameError] = React.useState<string | null>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);
  // blur 与「✕ 取消 / Enter 提交」会同时触发；用 ref 抢占，避免 blur 再跑一次提交。
  const settledRef = React.useRef(false);

  const filtered = searchQuery
    ? sessions.filter((s) =>
        s.title.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : sessions;

  const beginEdit = (session: ChatSession) => {
    setRenameError(null);
    settledRef.current = false;
    setEditingId(session.id);
    setDraft(session.title);
    // 打开即聚焦并全选（功能性自动聚焦，非辅助层——保留）。
    requestAnimationFrame(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    });
  };

  const cancelEdit = () => {
    settledRef.current = true;
    setEditingId(null);
    setDraft('');
  };

  const commitEdit = (session: ChatSession) => {
    if (settledRef.current) return;
    settledRef.current = true;
    const next = draft.trim();
    setEditingId(null);
    setDraft('');
    // 空值提交视作取消；无变化直接收工。
    if (!next || next === session.title) return;
    setRenameError(null);
    void renameSession(session.id, next).catch((err) => {
      setRenameError(describeRequestFailure(err, '重命名', '这个行程').message);
    });
  };

  if (filtered.length === 0) {
    return (
      // 在可用空间里居中，而不是 `py-12` 顶在一大片空白的最上面 —— 折叠/新建之后
      // 轨道中段常常是空的，一句贴在顶上的提示会把那片空白显得更大。
      <div className="flex h-full min-h-[8rem] flex-col items-center justify-center text-ink-secondary">
        <MessageSquare size={24} className="mb-2 opacity-40" />
        <span className="text-xs">{searchQuery ? '无匹配结果' : '暂无对话'}</span>
      </div>
    );
  }

  const sections: Array<[string, ChatSession[]]> = [...groupByDate(filtered)];

  return (
    <div className="space-y-4 pb-2">
      {renameError && (
        <div className="ml-[54px] mr-2 rounded-card bg-error/10 px-2.5 py-1.5 text-[11px] leading-snug text-error">
          {renameError}
        </div>
      )}
      {sections.map(([label, items]) => {
        return (
          <div key={label}>
            <div className="py-1 pl-[54px] text-[11px] font-medium uppercase tracking-wider text-ink-secondary">
              {label}
            </div>
            <div className="space-y-0.5">
              <AnimatePresence initial={false}>
                {items.map((session) => {
                  const isEditing = editingId === session.id;
                  return (
                    <m.div
                      key={session.id}
                      layout={false}
/* 列表退场：opacity + `translateY(-4px)`，base + accelerate，
                          无 layout prop（不给主 bundle 引入 domMax）。**不要**用
                          `height → 0` + `marginTop → 0`：那是两个**布局属性**，只放行
                          三条明码登记的布局动画。这段退场还有两处副本（我的偏好、测试
                          夹具），改规格时三处都要改到。 */
                      exit={{
                        opacity: 0,
                        y: -4,
                        transition: { duration: duration.base, ease: easing.accelerate },
                      }}
                    >
                      <div
                        className={cn(
                          'group flex items-center gap-1 rounded-card',
                          session.id === currentSessionId ? 'bg-accent/8 text-accent' : 'text-ink'
                        )}
                      >
                        {isEditing ? (
                          <div className="flex min-w-0 flex-1 items-center gap-1 py-1.5 pl-[54px] pr-2">
                            <input
                              ref={inputRef}
                              data-testid="rename-input"
                              value={draft}
                              onChange={(e) => setDraft(e.target.value)}
                              onBlur={() => commitEdit(session)}
                              onKeyDown={(e) => {
                                // 功能性 Enter 提交（保留）；不写 Esc（取消走 ✕ 钮）。
                                if (e.key === 'Enter') {
                                  e.preventDefault();
                                  commitEdit(session);
                                }
                              }}
                              /* 与后端唯一的标题上限对齐：
                                 `entities/session_title.TITLE_MAX_LEN = 32`。
                                 一列两个上限就是它们漂开的方式。 */
                              maxLength={32}
                              className={cn(
                                'flex-1 min-w-0 rounded-card bg-panel/70 px-1.5 py-1 text-sm text-ink',
                                'border border-accent/60',
                                ''
                              )}
                            />
                            {/* ✕ 取消：mousedown 抢在 input blur 之前，确保「点 ✕ = 取消」而非 blur 提交。 */}
                            <button
                              type="button"
                              data-testid="rename-cancel"
                              onMouseDown={(e) => {
                                e.preventDefault();
                                cancelEdit();
                              }}
                              className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-card text-ink-secondary transition-colors duration-fast ease-standard hover:bg-ink/8 hover:text-ink"
                            >
                              <X size={13} />
                            </button>
                          </div>
                        ) : (
                          <>
                            <button
                              type="button"
                              className={cn(
                                'touch-row flex flex-1 min-w-0 items-start gap-0 rounded-card py-2 pl-1.5 pr-2 text-left',
                                'transition-colors duration-fast ease-standard',
                                session.id === currentSessionId
                                  ? 'text-accent'
                                  : 'hover:bg-ink/[0.04]'
                              )}
                              onClick={() => {
                                dispatch({ type: 'SET_ACTIVE_VIEW', payload: 'chat' });
                                void openSession(session.id);
                              }}
                            >
                              <SessionRecord title={session.title} />
                            </button>
                            {/* coarse pointer 下操作簇常显（coarse-show），不依赖 hover。
                                coarse 下 coarse-cluster 把簇内间距提到 8px，coarse-op
                                把两钮视觉提到 32px（图标 14），中心距 40px、44px 热区仅重叠 4px。
                                fine pointer 维持 24px + hover 显现，不受影响。 */}
                            <div className="coarse-show coarse-cluster flex flex-shrink-0 items-center gap-0.5 opacity-0 transition-opacity duration-fast ease-standard group-hover:opacity-100">
                              {/* 两枚都只有字形，所以名字必须自己写：改名钮由
                                  `ui/Button` 的 icon 档在类型上要求，删除钮的触发器在
                                  `ui/ConfirmAction` 里由 `triggerLabel` 接。名字带上这一条
                                  是哪趟旅行 —— 一列十二条记录里十二枚同名的「删除」，念出来
                                  分不出删的是哪一条。 */}
                              <Button
                                variant="icon"
                                size="sm"
                                data-testid="rename-session"
                                aria-label={`重命名「${session.title}」`}
                                className="coarse-op !h-6 !w-6"
                                onClick={() => beginEdit(session)}
                              >
                                <Pencil size={12} />
                              </Button>
                              <ConfirmAction
                                testId="delete-session"
                                onConfirm={() => {
                                  void deleteSession(session.id);
                                }}
                                confirmLabel="删除"
                                triggerLabel={`删除「${session.title}」`}
                                triggerClassName="coarse-op"
                                className="border-transparent bg-transparent !px-0 !py-0"
                              >
                                <Trash2 size={12} />
                              </ConfirmAction>
                            </div>
                          </>
                        )}
                      </div>
                    </m.div>
                  );
                })}
              </AnimatePresence>
            </div>
          </div>
        );
      })}
    </div>
  );
};

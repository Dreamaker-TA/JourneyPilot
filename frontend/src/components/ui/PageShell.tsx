import React from 'react';
import { Search } from 'lucide-react';
import { m } from 'motion/react';
import { cn } from '../../lib/utils';
import { ChartMark, type ChartMarkName } from './ChartMark';
import { Neatline } from './Neatline';
import { Input } from './Input';
import { IdentityUnresolvedNotice } from './IdentityUnresolvedNotice';
import { RequestFailureNotice } from './RequestFailureNotice';
import { Skeleton } from './Skeleton';
import { staggerContainer, staggerItem } from '../../lib/motion';
import type { RequestFailure } from '../../lib/requestFailureMessage';

/**
 * 一个侧屏的**壳** —— 一根列、一套标题栏、一条状态顺序、一次入场编排。
 *
 * 三个侧屏（资料来源 / 旅行风格 / 我的偏好）与 `PageSkeleton` 共读这里的列宽、
 * 内边距、加载 / 失败 / 空态与入场次序。这些值**只在这里写一次**：每屏各写一份，就会漂成
 * 三个列宽、三种加载态。
 *
 * ── 列宽为什么必须留着字面 `max-w-*` 类名 ────────────────────────────────────
 * 正文列要靠 `[class*="max-w-"]` 找。换成 `.page-column`、`w-[64rem]` 或一个 CSS 变量，
 * 这个抓手就取不到正文列。
 *
 * ── 状态顺序 ────────────────────────────────────────────────────────────────
 * 身份 → 加载 → 失败 → 搜索没命中 → 真空 → 就绪。**身份排在加载之前**：那是一个决定
 * （不去读私有数据），不是一次等待。「搜索没命中」和「真空」是两句不同的话，图纸标记
 * 只挂在后者上。
 *
 * **没有任何一屏既带搜索又挂非空的 `mark`**：旅行风格两支都到得了但 `mark: null`，
 * 资料来源与我的偏好挂标记但没有搜索框。哪天有一屏同时要这两样，那半条逻辑要一起补上。
 *
 * ── 入场编排 ────────────────────────────────────────────────────────────────
 * 一个 stagger 容器挂在最外层，按文档顺序发车：标题 → 搜索 → 筛选 → 正文。变体经
 * motion 的 React context 往下传，所以正文那一格在滚动容器里也照样收得到。面自己
 * （背景、内边距、发丝线）**不动** —— 「pure decoration does not move」，
 * 而「一个面带着它的内容到场」才是那条规则说的 state change。
 *
 * ── 刊头印在图纸上，正文坐在一张白纸面上 ──────────────────────────────────────
 * 标题栏留在暖纸上（它是这一屏的刊头），正文是一张 `panel` 白纸面。
 *
 * **这张面是被它的描边定义的，不是被它的填色定义的。** `bg` #F5F1E4 与 `panel` #FCFBF6
 * 只差 **1.09:1**，所以单靠换填色，「正文是一个面」这件事在这张暖纸上根本看不见 —— 那正是
 * 四个侧屏会一路退化成「一张只有发丝线的纸」的机制。所以 `border-stroke` 是**必需项**，
 * `bg-panel` 只是让描边里侧不至于和图纸一模一样。这条与首屏两个入口定下的是同一条。
 *
 * 面是**图纸档**（半径 0，边界由描边 + `<Neatline />` 给出），不是一张大卡：卡的标准是
 * 半径 ≥ 8，所以这张面不会被数成「第二种卡面白」。
 *
 * ── 面向外溢出一个天沟，好让标题与正文站在同一条轴上 ────────────────────────────
 * `-mx-6 px-6`：面向左右各溢出 24px（正好是 `PAGE_GUTTER`），再用同样的 24px 把内容推
 * 回来 —— 于是**面内容的左缘 = 标题的左缘**，而面自己读起来是一条通栏的纸。
 *
 * 换成「面不溢出、内容靠内边距缩进」会让标题与正文差开 24px。当前写法（面溢出、内容用
 * 同样 24px 推回）让「面内容的左缘 = 标题的左缘」这一条一直成立。
 */

export type SurfaceState =
  | { kind: 'identity-unresolved' }
  | { kind: 'loading' }
  | { kind: 'error'; title: string; failure: RequestFailure; onRetry: () => void }
  /** 列表被查询筛成零，但内容是有的。不是空态，所以不挂标记。 */
  | { kind: 'search-miss'; line: string; hint?: string }
  /** 真的什么都没有。一屏一枚图纸标记就挂在这里（「single element deliberately」）。 */
  | { kind: 'empty'; mark: ChartMarkName | null; line: string; hint?: string }
  | { kind: 'ready' };

interface PageShellProps {
  /**
   * 面标题。**24px/700**。
   *
   * 20px 同时派给「面标题」与「一个面之内的区块标题」会让 我的偏好 上「我的偏好」和
   * 「旅行偏好」只能靠 700 与 600 的字重分，而区块标题实际落在 14px，比它管着的列表主词
   * （15px）**还小** —— 整条阶梯是倒的。24px 这一挡类型表里已经有（交付面的行程标题），
   * 一个侧屏的面标题和它是同一个角色，所以这里是**沿阶梯上移一档，不是新增一档**。
   */
  title: string;
  /** 这一屏是干什么的，一句话。13px（Compact labels 档），可省。 */
  purpose?: string;
  /** 标题右侧的读数（`12 趟` / `48 段资料`）。11px 等宽读数声部。 */
  readout?: React.ReactNode;
  /** 标题行右侧的动作簇。 */
  actions?: React.ReactNode;
  /** 有搜索就给这个；没有就不给（不是给一个空的）。 */
  search?: {
    value: string;
    onChange: (value: string) => void;
    placeholder: string;
  };
  /** 搜索下面的筛选行。 */
  tabs?: React.ReactNode;
  /** 身份未解析那句话里的那个名词（「你的行程」/「资料库状态」）。 */
  identitySurface: string;
  state: SurfaceState;
  children?: React.ReactNode;
}

/**
 * 一根列。三屏同读，`PageSkeleton` 也读它 —— 懒加载 chunk 落地那一刻列不会横跳。
 *
 * 1024px（`max-w-5xl`）：旅行风格那张三列卡格要 3×317px 才不挤，而单列列表再宽就开始
 * 出现一行右边 800px 空白。两个约束在这一档交汇，所以取它。
 */
export const PAGE_COLUMN = 'mx-auto w-full max-w-5xl';

/** 标题栏与正文共用的左右内边距。两处同读，不可能再漂开。 */
export const PAGE_GUTTER = 'px-6';

export const PageShell: React.FC<PageShellProps> = ({
  title,
  purpose,
  readout,
  actions,
  search,
  tabs,
  identitySurface,
  state,
  children,
}) => {
  // 标题栏里有几件东西，入场就排几步（正文整块算一格，它内部的行有自己的一层）。
  const steps = 2 + (search ? 1 : 0) + (tabs ? 1 : 0);

  return (
    <m.div
      className="flex h-full flex-col overflow-hidden"
      variants={staggerContainer(steps)}
      initial="hidden"
      animate="visible"
    >
      <div className={cn('flex-shrink-0 pb-4 pt-6', PAGE_GUTTER)}>
        <div className={PAGE_COLUMN}>
          {/**
           * 标题行。`pl-14 lg:pl-0`：小屏上 `MainLayout` 那枚 44px 的导航开关是
           * `fixed left-3 top-3`，会盖住标题（四屏 × 每一个手机宽度都撞，约 32×28px）。
           * 净空由壳统一留出，三屏同享。
           */}
          <m.div
            variants={staggerItem}
            className="flex min-w-0 items-start justify-between gap-4 pl-14 lg:pl-0"
          >
            <div className="min-w-0">
              {/* 副标题不放进 h1：`page-column.spec` 按 level 1 的可及名字找它。 */}
              <h1 className="text-2xl font-bold leading-tight text-ink">{title}</h1>
              {purpose && <p className="mt-1.5 text-[13px] text-ink-secondary">{purpose}</p>}
            </div>
            <div className="flex flex-shrink-0 items-center gap-3">
              {/* 读数**不走** `READOUT_LABEL`：那一套（等宽 + 0.12em 字距 + 全大写）是给
                  纯拉丁/纯数字的，而这里印的是 `9 个` / `48 段 · 3 源`。字距加在汉字后面
                  会把量词推开一格（实测 `21  段`），`uppercase` 对中文是空操作。
                  见 `ui/RuledList` 里 `RuledReadout` 头上那段。 */}
              {readout && <span className="text-[11px] tabular-nums text-ink-muted">{readout}</span>}
              {actions}
            </div>
          </m.div>

          {search && (
            <m.div variants={staggerItem} className="mt-4">
              <Input
                value={search.value}
                onChange={(event) => search.onChange(event.target.value)}
                placeholder={search.placeholder}
                icon={<Search size={15} />}
              />
            </m.div>
          )}

          {tabs && (
            <m.div variants={staggerItem} className="mt-3">
              {tabs}
            </m.div>
          )}
        </div>
      </div>

      {/* 滚动区与列都是 flex 列，好让正文面 `flex-1` 撑到视口底部（见下面那条说明）。 */}
      <div className={cn('flex flex-1 flex-col overflow-y-auto pb-6', PAGE_GUTTER)}>
        <m.div variants={staggerItem} className={cn('flex flex-1 flex-col', PAGE_COLUMN)}>
          {/**
           * 正文那张白纸面。
           *
           * 半径 0 + 描边 + `<Neatline />` 是图纸档的边界表达。描边是必需项，理由在文件头：
           * 纸与面只差 1.09:1，光换填色看不见。neatline 属于**结构性家具**，不占「一个
           * 视图一件采纳家具」的额度 —— 那条额度留给各屏自己在空态里挂的那一枚 `ChartMark`。
           *
           * 面自己**不动**（「the shell does not move」/「pure decoration does not
           * move」）：背景、内边距、发丝线一次画出，动的是它里面的行。
           *
           * `flex-1`：内容少的时候这张面仍然撑到视口底部。**不这么做，几个侧屏就有两种
           * 面** —— 资料来源那种长列表撑满，而一个空态只有 356px 高，读起来是一条悬在图
           * 纸上的横幅而不是这一屏的地面。同一件东西按内容多少长成两个样子，就是
           * 「一个角色两套值」的版面版本。内容超过一屏时它照旧跟着内容长（`flex-basis:auto`
           * 加上 `min-height:auto`，不会被压回一屏高）。
           */}
          <div className="relative -mx-6 flex flex-1 flex-col border border-stroke bg-panel px-6 py-7">
            <Neatline />
            {/* 内容排在 neatline（`z-0`）之上；`flex-1` 让空态那一格能在面里居中。 */}
            <div className="relative z-[1] flex flex-1 flex-col">
              <SurfaceBody state={state} identitySurface={identitySurface}>
                {children}
              </SurfaceBody>
            </div>
          </div>
        </m.div>
      </div>
    </m.div>
  );
};

const SurfaceBody: React.FC<{
  state: SurfaceState;
  identitySurface: string;
  children?: React.ReactNode;
}> = ({ state, identitySurface, children }) => {
  switch (state.kind) {
    case 'identity-unresolved':
      return <IdentityUnresolvedNotice surface={identitySurface} />;

    case 'loading':
      // 一种加载态：刻线行的骨架。**不是转圈** —— 一枚无字转圈说不出在等什么，
      // 而骨架占的是「将要出现的那个东西」的位（`ui/Skeleton` 的存在理由）。
      return (
        <div data-testid="surface-loading" className="divide-y divide-stroke/60">
          {[0, 1, 2, 3].map((row) => (
            <div key={row} className="flex items-center justify-between gap-4 py-4">
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <Skeleton radius="label" className={row % 2 ? 'h-4 w-1/3' : 'h-4 w-1/2'} />
                <Skeleton radius="label" className="h-3 w-2/5" />
              </div>
              <Skeleton radius="label" className="h-3 w-20 flex-shrink-0" />
            </div>
          ))}
        </div>
      );

    case 'error':
      return (
        <RequestFailureNotice title={state.title} failure={state.failure} onRetry={state.onRetry} />
      );

    case 'search-miss':
      return (
        <div className="flex flex-1 flex-col items-center justify-center py-16 text-center">
          <p className="text-base text-ink-secondary">{state.line}</p>
          {state.hint && <p className="mt-1.5 text-xs text-ink-muted">{state.hint}</p>}
        </div>
      );

    case 'empty':
       /**
        * 真空态。图纸标记与那句话是**同一个父级下的兄弟**，而这个父级里**一个 `svg`
        * 都不能有**：取这块时要断言 `svg` 计数为 0，所以这里不挂任何图标。
        *
        * 壳也不许自己画图纸家具：「one element deliberately」是**调用点**显式写下的
        * 一次采纳，而一个壳画在它包住的每一个视图上，那正是同一条规则里「by default」的
        * 定义。所以 `mark` 是参数，由每一屏自己传，且允许传 `null`
        * （旅行风格库就没有真空态 —— `preset/store.py` 的 `WHERE user_id = :uid OR
        * is_preset = TRUE` 保证九个官方风格对每个用户都返回）。
        */
      return (
        <div className="flex flex-1 flex-col items-center justify-center py-16 text-center">
          {state.mark && <ChartMark mark={state.mark} size={104} className="mb-5" />}
          <p className="text-base text-ink">{state.line}</p>
          {state.hint && <p className="mt-1.5 text-xs text-ink-secondary">{state.hint}</p>}
        </div>
      );

    case 'ready':
      return <>{children}</>;
  }
};

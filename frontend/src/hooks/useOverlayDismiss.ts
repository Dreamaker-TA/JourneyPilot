import { useEffect, useRef } from 'react';

/**
 * 浮层的「按 Esc 退出 + 焦点归还」—— **全站一处定义**。
 *
 * ## 它修的是什么
 *
 * 「Esc 关闭」与「焦点归还」曾被整类划进删除清单。`role="dialog"` + `aria-modal="true"`
 * 后来被拿回到 `ui/Modal` 时，这两样**没有跟着回来**，于是产品里长期存在一枚自称对话框、
 * 按 Esc 不动的弹层：实测两枚独立的 T3 确认弹层（删除资料库 / 删除全部记忆）按 Esc 前后
 * `[role=dialog]` 计数都是 1，唯一的退路是 Tab 到 ✕ 或「取消」再回车。而 `aria-modal="true"`
 * 对读屏软件是一句承诺，「Esc 能出来」就在这句承诺里。
 *
 * ## 为什么是一处，而不是三处
 *
 * 探查时实测到的存量是**四份各写一遍的 Esc**：`ui/Popover`（藏在 `closeOnEscape` 这个
 * 出厂 `false` 的开关后面，7 个使用点里只有 4 个打开它）、`DateRangePicker`、
 * `BundleCitationMarker` 的全屏来源详情，加上 `ui/Modal` 与 `ui/Sheet` 的**零份**。
 * 同一件事在四处各写一份、其中一份静默胜出。现在三枚浮层原语都调这一处，
 * `closeOnEscape` 那个开关删掉了：**Esc 不是一个可选项**。
 *
 * ## 两条机制上的细节，别把它们简化掉
 *
 * 1. **只有最上面那一层浮层吃这一下 Esc。** 模块级的 `stack` 记着当下开着的浮层，
 *    键盘监听挂在 `document` 上 —— 不判栈顶的话，一次 Esc 会把弹层里那枚下拉和弹层本身
 *    一起关掉（PresetCreator 的表单里就有 `SelectMenu`）。栈按打开顺序进出，最后进来的
 *    先出。
 * 2. **归还的对象是「打开它之前焦点在哪」，不是「哪个 ref 传进来了」。** 前者对三枚原语
 *    都成立、也对键盘打开与鼠标打开都成立；后者要每个原语再声明一遍自己的触发器，
 *    而那就是第二份账。元素若已从文档里消失（列表行连同它的钮一起被删）就不归还 ——
 *    往一个 detached 节点上 `focus()` 是无声无效的，而那种无声会被当成「归还过了」。
 */

/** 当下开着的浮层，按打开顺序。只有栈顶那一层响应 Esc。 */
const openOverlays: symbol[] = [];

interface OverlayDismissOptions {
  /** 浮层此刻是否开着。 */
  open: boolean;
  /** Esc 按下时调用（由消费方决定怎么关）。 */
  onDismiss: () => void;
  /**
   * 模态面板的 ref。**给了它就等于声明「这是一枚模态面**」：打开时焦点进面板，
   * 于是 Tab 从面板内部开始走、Esc 之后的归还也看得见。
   *
   * `ui/Popover` 不给 —— 锚定浮层是非模态的，焦点留在触发器上才是它的语法。
   * 面板自己要带 `tabIndex={-1}`，否则 `focus()` 落不上去（一个静默失败）。
   */
  panelRef?: React.RefObject<HTMLElement>;
}

export function useOverlayDismiss({ open, onDismiss, panelRef }: OverlayDismissOptions): void {
  // 回调放进 ref：否则消费方每次 render 造一个新函数都会让下面的 effect 重挂一遍，
  // 而重挂会把这一层从栈里弹出再压回栈顶 —— 嵌套时的栈序就随 render 抖动了。
  const onDismissRef = useRef(onDismiss);
  onDismissRef.current = onDismiss;

  useEffect(() => {
    if (!open) return undefined;

    const id = Symbol('overlay');
    openOverlays.push(id);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (openOverlays[openOverlays.length - 1] !== id) return;
      event.preventDefault();
      onDismissRef.current();
    };
    document.addEventListener('keydown', onKeyDown);

    return () => {
      document.removeEventListener('keydown', onKeyDown);
      const index = openOverlays.indexOf(id);
      if (index >= 0) openOverlays.splice(index, 1);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;

    const opener = document.activeElement as HTMLElement | null;

    if (panelRef) {
      // 下一帧再抢焦点：React 会在 commit 里处理子元素的 `autoFocus`（表单弹层靠它把
      // 焦点放进第一个字段，那是明确保留的功能性自动聚焦）。已经在面板里就不动它。
      const frame = requestAnimationFrame(() => {
        const panel = panelRef.current;
        if (panel && !panel.contains(document.activeElement)) panel.focus();
      });
      return () => {
        cancelAnimationFrame(frame);
        if (opener && document.contains(opener)) opener.focus();
      };
    }

    return () => {
      if (opener && document.contains(opener)) opener.focus();
    };
    // `panelRef` 是稳定的 ref 对象，不进依赖 —— 进了它也不会变。
  }, [open]);
}

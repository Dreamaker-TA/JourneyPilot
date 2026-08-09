/**
 * 侧栏那一行讲的是什么 —— 把一条会话标题读回它的**字段**。
 *
 * 标题的**产出**只有一处权威：后端 `entities/session_title.py`。它给一趟旅行的会话
 * 命名为 `<出发地> → <目的地> · <日期>`，给一个问题命名为问题本身。这个模块是那条
 * 形状的**读者**，不是第二个产出方：它一个字符串都不生成，只回答「这一行里哪一段是
 * 目的地、哪一段是日期」。
 *
 * 为什么要读回字段：目的地是加粗的主词，出发地降为前缀，日期自己成为一行等宽读数。把整条
 * 标题当**一句话**印的话，同一条路线的两趟旅行唯一的区别（尾部日期）会被排到第二行行末，
 * 而目的地 —— 真正要扫的那个词 —— 和出发地、箭头、日期一样重；280px 的栏宽还会把
 * `东京都/東京都` 从中间断开，印成 `东京都/東京` + `都 · 8/5–8/8`。
 *
 * 认不出这个形状的标题（问题、用户自己重命名过的名字）就**原样印一行**。这不是
 * 「缺字段走旧逻辑」：标题是一个可被用户任意重命名的自由文本字段，它没有第二份合同，
 * 认不出结构时把它原样印出来是唯一诚实的呈现。
 */

/** 日期尾段：`8/5` 或 `8/5–8/8`。破折号与后端 `_DATE_RANGE_DASH` 同一个字符（U+2013）。 */
const DATE_TAIL = /^\d{1,2}\/\d{1,2}(?:–\d{1,2}\/\d{1,2})?$/;

/** 与后端 `_ROUTE_ARROW` / 标题内的 ` · ` 分隔逐字一致。 */
const ROUTE_ARROW = ' → ';
const FIELD_SEPARATOR = ' · ';

export type ParsedSessionTitle =
  | { kind: 'trip'; origin: string | null; destination: string; dates: string | null }
  | { kind: 'plain'; text: string };

export function parseSessionTitle(title: string): ParsedSessionTitle {
  const text = title.trim();
  if (!text) return { kind: 'plain', text: title };

  // 日期在**尾部**，所以从最后一个 ` · ` 切；地名本身理论上也可能含分隔符，
  // 按严格的日期形状校验尾段，认不出就当整条都是路线部分。
  let head = text;
  let dates: string | null = null;
  const lastSeparator = text.lastIndexOf(FIELD_SEPARATOR);
  if (lastSeparator > 0) {
    const tail = text.slice(lastSeparator + FIELD_SEPARATOR.length);
    if (DATE_TAIL.test(tail)) {
      head = text.slice(0, lastSeparator);
      dates = tail;
    }
  }

  const arrowAt = head.indexOf(ROUTE_ARROW);
  const origin = arrowAt > 0 ? head.slice(0, arrowAt).trim() : null;
  const destination = arrowAt > 0 ? head.slice(arrowAt + ROUTE_ARROW.length).trim() : head;

  // 一条能拆的行至少要有箭头或日期其中之一；两个都没有就是一句话，
  // 不能把「日本签证要准备什么材料」讲成一个目的地。
  if (!origin && !dates) return { kind: 'plain', text };
  if (!destination) return { kind: 'plain', text };

  return { kind: 'trip', origin, destination, dates };
}

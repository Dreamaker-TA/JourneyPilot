import { cn } from '../../lib/utils';
import { splitLabelledFact } from '../../lib/tripDomains';
import { READOUT_LABEL } from '../../lib/typography';

/**
 * 一趟旅行的硬事实怎么排 —— 报告封面与工作台总览共用的两个形状。
 *
 * 目的地 / 行程期间 / 天数 / 费用 四格读数：标签等宽小字、值在 ink，四格一条发丝线隔开。
 * 全程亮点同理，投影层给的是 `标签：值`（`trip_highlights` 里逐条 `f"{label}：{body}"`），
 * 按全角冒号拆成两级排。
 *
 * **两个面读同一份实现，谁也不许自己再拼一遍。** 拼成一句话就会坏两次：
 *
 * - `[dateRange, destinations.join(' · '), '4 天', costStatement].join(' · ')` 得到
 *   `10月3日 – 10月6日 · 东京 · 京都 · 4 天 · 预计合计 ¥5,600` —— **两级结构用同一个分隔符
 *   印**，读者分不出「东京 · 京都」是两个目的地、而「4 天」是另一类事实；目的地是一个列表
 *   这件事在串里彻底消失。
 * - `.slice(0, 2)` 之类的截断只印四条里的两条、又不留余数提示，等于把「我们还有两条」藏起来。
 */

/**
 * 一格读数：标签等宽 + 字距，值在 ink。
 *
 * 刻意**不加** `tabular-nums`：这些值全是中英混排（`10月3日 – 10月6日`、`4 天`、
 * `已知费用 ¥176 · 整趟预算估算 ¥1,384`），而 CJK 字体在等宽数字下会把全角标点也撑成
 * 数字宽。等宽数字只给纯数字列（时刻、金额），那些在时间轴上。
 *
 * 值是 **15px**，和同一个文件里 `LabelledFactList` 的值同挡：报告封面这四格（目的地 / 行程
 * 期间 / 天数 / 费用）和它下面「全程亮点」那四格是**同一个角色** —— 一条硬事实的值 —— 必须
 * 同字号。§Typography 的 15px 那一挡写的正是「field values、列表行主词、报告 lede —— 阅读挡」。
 *
 * Report 口径把这里叫「a four-cell mono readout」，但等宽只给**标签**：四个值里有
 * `东京 · 京都`，等宽给中文排版就是错的（同 `RuledReadout` 那一条）。
 */
export function TripFactReadout({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className={cn(READOUT_LABEL, 'text-ink-muted')}>{label}</dt>
      <dd className="mt-1.5 break-words text-[15px] font-medium leading-6 text-ink">{value}</dd>
    </div>
  );
}

/**
 * `标签：值` 那一批（全程亮点）拆成两级排成两列。
 *
 * 标签走 chart 印刷蓝（chart 是非交互强调声部），值走 ink。刻意**不**按标签
 * 文字反推域再配域色：那是把界面钉在后端文案上，`_VISIT_LABEL` 改一个字这里就静默退化。
 * 标签是什么字由后端说，这里只负责分两级。
 *
 * 拆不开的整条当值印（`splitLabelledFact` 的口径），不假设形状。
 */
export function LabelledFactList({
  facts,
  testId,
}: {
  facts: readonly string[];
  testId?: string;
}) {
  return (
    <dl data-testid={testId} className="grid gap-x-8 sm:grid-cols-2">
      {facts.map((fact, index) => {
        const { label, value } = splitLabelledFact(fact);
        return (
          <div
            key={fact}
            className={cn(
              'min-w-0 border-t border-stroke/60 py-3',
              index === 0 && 'border-t-0 pt-0',
              index === 1 && 'sm:border-t-0 sm:pt-0',
            )}
          >
            {label && (
              <dt className={cn(READOUT_LABEL, 'text-chart')}>{label}</dt>
            )}
            <dd className="mt-1.5 break-words text-[15px] font-medium leading-6 text-ink">{value}</dd>
          </div>
        );
      })}
    </dl>
  );
}

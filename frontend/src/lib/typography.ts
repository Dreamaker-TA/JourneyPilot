/**
 * 读数标签的声部 —— 一处定义，全部交付面同读。
 *
 * 这个声部（等宽 + 小字号 + 字距 + 全大写）是一个**声部**，值只写一次。抄到调用点上它会
 * 漂开：字号变成 10px 和 11px 两挡、字距变成 `0.1em` 和 `0.14em` 两个值 —— 和
 * `--chart-day-N`、`TRIP_DOMAIN_PRESENTATION`、`describeRequestFailure` 同一条纪律。
 *
 * 颜色不进这里：`ink-muted` 是中性读数、`chart` 是印刷强调，两种都在用，由调用方给。
 *
 * **字号 11px，而且它是全站字号的地板**（合同 §Typography 已收录）。标签不许再往下压；
 * 句子（不是标签）走 12px。
 */
export const READOUT_LABEL = 'font-mono text-[11px] font-semibold uppercase tracking-[0.12em]';

/* ─── 一个面之内的三档标题 ────────────────────────────────────────────────────────────
   四个侧屏共读这三个常量。此前 `SECTION_TITLE` / `SECTION_NOTE` 各屏自己写一份，而
   §Typography 的三个标题角色（20px 面标题 / 14-600 区块标题 / 15-600 行主词）在实现里
   塌成了 **20 → 14 → 14 → 14**：区块标题、卡标题、卡内标题、行标题全是 14/600。

   修法是**把阶梯整体上移一档**，不是发明新字号 —— 下面每一个值类型表里都已经有：

     24/700  面标题（`PageShell`）      ← 原 20，与「交付面的行程标题」同一角色
     20/600  区块标题 SECTION_TITLE     ← 原 14，比它管的列表主词（15）还小，是倒的
     14/600  组标题  GROUP_LABEL        ← 原 12，与它标注的选项同号同色，等于没有标注
     13      说明句  SECTION_NOTE       ← 原 12（元数据档）；一句要读的话不是元数据

   「一个区块标题比它管着的行还小」是这一屏读不出结构的**根**：告诉你在看什么的那几个
   词是全屏最小最淡的字。所以判断一个字号对不对，看的是它和**它管着的东西**的关系，
   不是它自己在表里存不存在。 */

/** 一个面之内的区块标题。20px/600。 */
export const SECTION_TITLE = 'text-xl font-semibold leading-snug text-ink';

/** 区块标题下面那一句。13px。 */
export const SECTION_NOTE = 'mt-1.5 text-[13px] leading-relaxed text-ink-secondary';

/**
 * 区块之内的一组的名字（六组偏好的组名）。14px/600。
 *
 * **不走 `READOUT_LABEL`**：那一套（等宽 + 0.12em 字距 + 全大写）是给纯拉丁/纯数字的，
 * 「旅行风格」四个汉字印出来会变成字距撑开的一行，而 `uppercase` 对中文是空操作。
 */
export const GROUP_LABEL = 'text-sm font-semibold text-ink';

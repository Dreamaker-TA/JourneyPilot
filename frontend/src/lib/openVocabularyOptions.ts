/**
 * 一份**开放词表**里存下来的值怎么摆上屏幕。
 *
 * 「后端存的是自由字符串，而前端按一份固定表判命中」会让一个**存在的
 * 值渲染成零枚高亮**、而且点不掉。六组偏好的答案是把词表闭合、定义处放后端、下发给界面
 * （`lib/preferenceGroups.ts`）—— 那条路成立的前提是**界面本来就只让用户从表里挑**。
 *
 * 旅行风格预设那几个字段不满足这个前提：`PresetConstraints` 的
 * `budget` / `pace` / `output_style` / `focus_areas` 是自由文本，写入方除了这一屏还有
 * **AI 生成**那一路（`POST /api/presets/generate-instructions`，模型自己造词），
 * 而九个官方风格的 `focus_areas` 本身就写着「小吃 / 市场 / 烹饪体验 / 便捷交通 /
 * 米其林餐厅 / 出片 / 取景地」这类表外的词。所以它们是开放词表，而开放词表的规则是另一条：
 *
 *   **存下来的值本身就是唯一的真相；屏幕上必须看得见它。固定表只是快捷入口，不是判据。**
 *
 * 不这么办的实测后果：`ui/SelectMenu` 的 `options.find(o => o.value === value)` 找不到
 * 时印的是 placeholder（「不限」）—— 一个设了「适中」节奏的预设看起来像**没设**；
 * 关注领域那一格更狠，18 枚固定 chip 里没有「小吃」，于是它既不显示、也**点不掉**，
 * 而保存时又原样写回去，与多选组「存的值画不出来」逐字同形。
 */

/**
 * 关注领域的 chip 顺序：先固定的建议项，再补上存下来的、建议项里没有的那几个。
 *
 * 不去重成 Set 再排序 —— 建议项的顺序是有意的（同类挨着），而后补的那几个按它们
 * 存下来的顺序排，读者能看出「这几个不是从上面挑的」。
 */
export function focusAreaChips(
  suggestions: readonly string[],
  stored: readonly string[],
): string[] {
  const extras = stored.filter((value) => value && !suggestions.includes(value));
  return [...suggestions, ...Array.from(new Set(extras))];
}

/**
 * 一个下拉的取值列表，必然包含当前存着的那个值。
 *
 * 空串是「不限」，它本来就在列表里，所以不会被重复加一次。
 */
export function selectValuesIncludingStored(
  values: readonly string[],
  stored: string | null | undefined,
): string[] {
  if (!stored || values.includes(stored)) return [...values];
  return [...values, stored];
}

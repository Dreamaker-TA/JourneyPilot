import type { PreferenceOptionGroup, UserProfile } from '../types/api';

/**
 * 「我的偏好」那六组 chip 的模型：**选项从服务端来，选中判定对着同一份表**。
 *
 * 这里的缺陷形状是同一件事在两处各有一份取值（前端一份、后端一份）：
 *
 * - 后端 `PATCH /users/{id}/preferences` 收任意字符串、不查表也不归一化；
 * - 这一屏自己带着一份固定选项表，按 `chosen.includes(option)` 判 chip 亮不亮。
 *
 * 于是两份表可以漂开，而**漂开的后果不是报错、是隐身**：实测 owner 六组里五个值不在
 * 这一屏的表里（`文化` / `中档` / `悠闲` / `酒店` / `不吃香菜`），页面只有一枚 chip
 * 高亮（`高铁`，唯一凑巧字面相同的那个），数据全在库里；多选组的 `toggleOption` 是在
 * 当前数组上追加/移除，而没有一枚 chip 字面等于 `文化`，所以那些隐藏值**永远点不掉**。
 *
 * 修法不是在这里给表外的值兜一条底（那就是双读，正是本仓禁的兼容层），而是让**表只有
 * 一份**：后端定义、后端校验、`GET /users/preference-options` 下发，这一屏渲染它。
 * 于是「存下来的值一定画得出来」由后端保证，这个模块只负责把它摆成 chip。
 *
 * 逻辑放在 lib 而不是那个 `.tsx` 里：它不依赖组件与 DOM，任何读者都能量到。
 */

/** 一组偏好在屏幕上的样子：每一枚 chip 及它亮不亮。 */
export interface PreferenceChipGroup {
  key: string;
  label: string;
  multi: boolean;
  chips: Array<{ option: string; selected: boolean }>;
}

/** 这一屏的编辑态：一组一个已选值数组（单选组最多一项）。 */
export type PreferenceSelections = Record<string, string[]>;

/**
 * 从画像里读出每一组当前已选的值。
 *
 * **不在这里过滤表外的值**：后端保证存下来的值出自选项表，真出现表外的值说明那道保证
 * 破了 —— 那时该让它在下面 `preferenceChipGroups` 的判据里显形（chip 数对不上），
 * 而不是在这里悄悄擦掉，让一次真实的漂移变成一次看不见的数据丢失。
 */
export function readPreferenceSelections(
  groups: PreferenceOptionGroup[],
  profile: UserProfile | null,
): PreferenceSelections {
  const stored = (profile?.preferences ?? {}) as Record<string, unknown>;
  const selections: PreferenceSelections = {};
  for (const group of groups) {
    const value = stored[group.key];
    if (Array.isArray(value)) selections[group.key] = value.map(String);
    else if (typeof value === 'string' && value.trim()) selections[group.key] = [value];
    else selections[group.key] = [];
  }
  return selections;
}

/** 服务端给的选项表 + 当前编辑态 → 屏幕上那几排 chip。 */
export function preferenceChipGroups(
  groups: PreferenceOptionGroup[],
  selections: PreferenceSelections,
): PreferenceChipGroup[] {
  return groups.map((group) => {
    const chosen = selections[group.key] ?? [];
    return {
      key: group.key,
      label: group.label,
      multi: group.multi,
      chips: group.options.map((option) => ({ option, selected: chosen.includes(option) })),
    };
  });
}

/**
 * 点一枚 chip 之后的新编辑态。
 *
 * 单选组（后端是 `str`）再点一次取消、点别的替换；多选组追加/移除。
 */
export function togglePreferenceOption(
  group: PreferenceOptionGroup,
  selections: PreferenceSelections,
  option: string,
): PreferenceSelections {
  const current = selections[group.key] ?? [];
  const updated = group.multi
    ? current.includes(option)
      ? current.filter((value) => value !== option)
      : [...current, option]
    : current.includes(option)
      ? []
      : [option];
  return { ...selections, [group.key]: updated };
}

/**
 * 组装保存 payload：**键就是服务端给的那几组**，类型按 `multi` 给（多选发数组、
 * 单选发字符串）。
 *
 * 这一屏不管的 `default_origin` 有自己的 API，所以它不在选项表里、也不出现在 payload
 * 里 —— 后端只替换 payload 里出现的键，常用出发地不会被这次保存清空。
 *
 * 全部取消勾选时**仍然发全部六个键**（单选发空串、多选发空数组），否则「取消勾选」在
 * 一个「带上来的键才覆盖」的后端上等于什么都没做。
 */
export function buildPreferencePayload(
  groups: PreferenceOptionGroup[],
  selections: PreferenceSelections,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const group of groups) {
    const values = selections[group.key] ?? [];
    payload[group.key] = group.multi ? values : (values[0] ?? '');
  }
  return payload;
}

/**
 * 「用户当前选的旅行风格」这件事的持久化，全前端唯一一处。
 *
 * 为什么要持久化：选择此前只活在 `AppContext` 的内存里，刷新一次页面就**静默**变回
 * 「没选」——屏幕上那枚风格 chip 消失，而下一次发消息的 `preset_id` 变成 null，
 * 用户不会收到任何提示。这和开机兜底捡最近一段会话是同一个位置的
 * 反面：一个用户的显式选择，被一个隐式默认在时间上后发地覆盖掉。
 *
 * 存 id **与** name 两个字段，而不是只存 id：只存 id 时，页面回来后 chip 要等
 * 用户点开下拉、`listPresets()` 回来才知道该印什么字，中间那一段是一枚空白 chip。
 * 名字可能过期（用户改过风格名），所以 `PresetSelector` 打开时那次列表加载**同时**
 * 负责对账：id 不在列表里就清掉并出声，名字漂了就就地改正。存的是快照，
 * 权威永远是服务端那份列表。
 */

const ACTIVE_PRESET_KEY = 'sta_active_preset';

export interface StoredActivePreset {
  id: string;
  name: string;
}

export const readStoredActivePreset = (): StoredActivePreset | null => {
  try {
    const raw = localStorage.getItem(ACTIVE_PRESET_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null) return null;
    const { id, name } = parsed as Record<string, unknown>;
    if (typeof id !== 'string' || id.trim() === '') return null;
    return { id, name: typeof name === 'string' ? name : '' };
  } catch {
    return null;
  }
};

export const writeStoredActivePreset = (preset: StoredActivePreset | null): void => {
  try {
    if (!preset) {
      localStorage.removeItem(ACTIVE_PRESET_KEY);
      return;
    }
    localStorage.setItem(ACTIVE_PRESET_KEY, JSON.stringify(preset));
  } catch {
    // 存储不可用（隐私模式 / 配额）时选择仍在本次会话内有效，只是不跨刷新。
  }
};

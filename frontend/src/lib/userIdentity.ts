/**
 * 这个浏览器是哪个用户 —— **身份解析只有这一处答案**。
 *
 * 产品没有登录：`user_id` 由客户端自己声明，第一次打开时生成一个 `u_…` 存进
 * localStorage。这条规则的代价是**身份跟着浏览器走**：换一个浏览器、换一台机器、
 * 换一个隐私窗口，就是另一个用户，偏好、记忆、资料库、旅行历史全是空的 ——
 * 数据在库里好好躺着，只是问的人换了。
 *
 * 所以这里多给一条入口：`?user=<id>`。它存在的唯一理由是**让一个身份可以用一条链接
 * 带到任何一个浏览器上**（演示、换机器、给别人看同一套数据）。取值范围与后端一致
 * （`rag/collections.py::user_scoped_collection` 那套清洗规则：字母数字加下划线连字符，
 * 且不许是 `anonymous`），不合法就当没写 —— **不许把一个非法 id 静默改写成一个合法的**，
 * 那会让人以为自己看的是 A 的数据而其实是 B 的。
 *
 * 优先级只有一种顺序，写在 `resolveUserId` 里：**URL > 已存的 > 生成一个新的**。
 * URL 那一支会**写回 localStorage**，所以带参数打开一次之后，之后直接开首页也还是这个身份
 * （要换回来就再带一次参数，或者清掉 localStorage）。
 */

/** localStorage 里存身份的键。**这是它的定义处**，别处 import，不要再写一遍字面量。 */
export const USER_ID_STORAGE_KEY = 'sta_user_id';

/** 用一条链接带身份的查询参数名，和 `?view=` 同一族（都是界面层参数，不是 API 参数）。 */
export const USER_ID_QUERY_PARAM = 'user';

/**
 * **只用来判 `?user=` 那一支**：后端清洗规则里活得下来、且不是 `anonymous`。
 *
 * `anonymous` 在这个产品里**是一个有意义的值**，不是垃圾：它是「身份没解析出来」的哨兵
 * （`AppContext` 的 `userIdentityReady` 就是 `userId !== 'anonymous'`，四个侧屏靠它印出
 * 「没有可用的用户身份，这里不读取…」）。所以它只在 URL 那一支被拒 —— 一条链接是「请让我
 * 成为这个用户」的请求，而 `anonymous` 不是一个用户。
 */
const VALID_USER_ID = /^[A-Za-z0-9_-]{1,64}$/;

export function isValidUserId(value: string | null | undefined): boolean {
  if (!value) return false;
  return VALID_USER_ID.test(value) && value !== 'anonymous';
}

export interface ResolvedUserId {
  /** `null` = 这台浏览器上还没有身份，调用方生成一个新的。 */
  userId: string | null;
  /** 是否需要写回 localStorage（URL 带来的身份要落地，否则下一次刷新又变回去）。 */
  persist: boolean;
  source: 'url' | 'storage' | 'none';
}

/**
 * 纯函数，故意不读 `window` —— 身份解析与引擎、组件都解耦，任何调用方注入 search
 * 与 stored 即可复现。
 *
 * @param search `window.location.search`
 * @param stored localStorage 里已存的身份（没有就传 null）
 */
export function resolveUserId(search: string, stored: string | null): ResolvedUserId {
  const fromUrl = new URLSearchParams(search).get(USER_ID_QUERY_PARAM);
  if (isValidUserId(fromUrl)) {
    return { userId: fromUrl as string, persist: true, source: 'url' };
  }
  // **已存的值原样使用，不做清洗。** 这里曾经也拿 `isValidUserId` 卡它一道，结果把
  // `anonymous` 这个哨兵当垃圾扔了、当场生成一个全新用户 —— 于是「身份没解析出来」这个
  // 真实状态被悄悄换成了「一个崭新的、什么都没有的用户」，四个侧屏那句诚实的说明再也不会
  // 出现（`identity-unresolved.spec.ts` 四条当场红，就是它抓住的）。
  // 一个值是不是「可用身份」由 `AppContext::userIdentityReady` 一处判定，不在这里判第二遍。
  if (stored) {
    return { userId: stored, persist: false, source: 'storage' };
  }
  return { userId: null, persist: true, source: 'none' };
}

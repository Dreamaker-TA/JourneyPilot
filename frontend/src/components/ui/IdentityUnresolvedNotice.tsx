import React from 'react';

/**
 * 「这一页不读你的私有数据」——一句陈述，不是一次等待。
 *
 * 三个侧屏（资料来源 / 旅行风格 / 我的偏好）在身份没解析出来时都**主动决定
 * 不发请求**：读数函数第一行就是 `if (!state.userIdentityReady) return;`。那一支必须落到
 * 这句陈述上，**不能落到加载态**：`loading` 初值是 `true`，而 `return` 排在「把 loading
 * 关掉」之前，屏幕就会永远停在初始那一帧 —— 一枚无字转圈五分钟后仍是它。
 *
 * 那样等于**把一个决定画成一次等待**：「我正在读」和「我决定不读」两种完全不同的处境共用
 * 一个外观，而这个外观说的是假话。合同两句都盖着它 —— §Motion「progress indicators must
 * degrade to a stalled state on timeout, never spin forever」，§Anti-Slop「No fake
 * precision: live data, degraded connections, and unavailable sources must be labeled
 * honestly」。
 *
 * **一份定义三处同读**，不许三处各写一遍。
 *
 * 一句话，不是三句。§UX Copy 的「三问」是给**聊天错误**的，而这里没有可给的下一步——
 * 浏览器每次都自己造一个 `u_<随机>` 身份（`useSessionManager`），`anonymous` 这个占位值
 * 前端从不产生——所以再补一段「你可以试试刷新……」就是防御性说辞，而不是信息。
 */
export const IdentityUnresolvedNotice: React.FC<{ surface: string }> = ({ surface }) => (
  <p
    data-testid="identity-unresolved-notice"
    className="rounded-card bg-surface px-3 py-2.5 text-xs leading-relaxed text-ink-secondary"
  >
    没有可用的用户身份，这里不读取{surface}。
  </p>
);

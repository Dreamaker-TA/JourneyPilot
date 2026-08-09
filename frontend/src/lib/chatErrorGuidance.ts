export type ChatErrorAction = 'retry' | 'reload' | 'edit' | null;

/**
 * 失败卡的两种来源，由消息类型结构化决定，不靠正文文本猜：
 * - `fault` = 系统故障，走下面的原始错误投影阶梯，可以给恢复动作；
 * - `guard_blocked` = 输入安全策略拒绝，是一次策略决定而非故障，一律不给动作。
 */
export type ChatErrorKind = 'fault' | 'guard_blocked';

export interface ChatErrorGuidance {
  title: string;
  description: string;
  action: ChatErrorAction;
  actionLabel?: string;
}

function normalizedError(raw: string): string {
  return raw
    .replace(/^请求失败\s*[:：]\s*/i, '')
    .replace(/^error\s*[:：]\s*/i, '')
    .trim();
}

/**
 * 把后端 / 网络原始错误投影成普通用户可执行的恢复路径。
 * 原文可由 MessageBubble 在检查面（圆圈 i）折叠展示，不进入普通用户正文。
 */
export function getChatErrorGuidance(raw: string, kind: ChatErrorKind): ChatErrorGuidance {
  // 安全策略拒绝先于一切错误匹配收口：它没有原始错误可投影，也没有可恢复路径。
  // 这里不提供任何动作——重发同一段内容仍会被同一条策略拒绝，给「再试一次」是误导。
  if (kind === 'guard_blocked') {
    return {
      title: '这个请求被安全策略拒绝',
      description: '重新发送同样的内容仍会被拒绝。换一个旅行规划相关的问题即可继续。',
      action: null,
    };
  }

  const message = normalizedError(raw);

  if (/\b(404|405)\b|not found|method not allowed/i.test(message)) {
    return {
      title: '旅行助手没有正确连接',
      description: '请重新启动 JourneyPilot，等待服务就绪后刷新这个页面。直接重新发送不会解决这个问题。',
      action: 'reload',
      actionLabel: '刷新页面',
    };
  }

  if (/\b(401|403)\b|unauthorized|forbidden|api[ _-]?key|authentication/i.test(message)) {
    return {
      title: '旅行助手暂时不可用',
      description: '请稍后重试；如果问题持续出现，请刷新页面后重新开始。',
      action: 'reload',
      actionLabel: '刷新页面',
    };
  }

  if (/\b(400|422)\b|消息内容不能为空|validation error|invalid request/i.test(message)) {
    return {
      title: '还需要调整一下行程需求',
      description: '请补充或修改目的地、出发时间、天数等信息后重新发送。',
      action: 'edit',
      actionLabel: '修改需求',
    };
  }

  if (/\b429\b|too many requests|rate limit/i.test(message)) {
    return {
      title: '现在请求有点多',
      description: '你的行程需求已经保留，稍等片刻后再试一次。',
      action: 'retry',
      actionLabel: '再试一次',
    };
  }

  if (/无法连接|failed to fetch|networkerror|network error|load failed|connection refused/i.test(message)) {
    return {
      title: '暂时连不上旅行助手',
      description: '请确认网络连接和 JourneyPilot 服务正常，稍等片刻后再试。',
      action: 'retry',
      actionLabel: '再试一次',
    };
  }

  if (/timeout|timed out|超时/i.test(message)) {
    return {
      title: '这次规划花的时间有点久',
      description: '可以再试一次；如果仍然超时，先缩短行程天数或减少同时查询的内容。',
      action: 'retry',
      actionLabel: '再试一次',
    };
  }

  // 用旧合同保存的结果：读多少次都是同一个答案，所以既没有「更新的一版」可看，
  // 也没有可重试的东西。恢复路径只有重新规划。
  if (/bundle_contract_superseded/i.test(message)) {
    return {
      title: '这趟旅行的结果无法展示',
      description: '它是用旧版本的结果格式保存的，现在已经读不出来了。请重新规划这趟旅行；重新载入不会有帮助。',
      action: null,
    };
  }

  // 断点续跑被拒：这次运行的公开投影失败了，而投影是确定性的（只读 Bundle，不碰时钟、
  // 供应商与随机数），所以续跑必然得到同一个结果。必须收在下面那条泛 409 之前：那条说
  // 的是「行程刚刚更新过，去看最新那一版」，而这里没有更新的一版可看，恢复路径是重新
  // 规划。给 `retry` 或 `reload` 都是教用户去撞同一面墙。
  if (/delivery_integrity_projection_failure/i.test(message)) {
    return {
      title: '这次运行的结果无法展示',
      description: '这趟旅行的结果没能生成可以展示的方案，继续这次运行会得到同一个结果。请重新规划这趟旅行。',
      action: null,
    };
  }

  // 409 = 服务端拒绝了这次写入，因为行程已经不是发起操作时看到的那一版。
  //
  // **必须**收口在 5xx 与 delivery-integrity 之前：它更具体，而「conflict」这类字样在更宽的
  // 分支里会被当成故障。漏到文件末尾的终态兜底就会拿到一个 `retry` 按钮 —— 等于教用户去
  // 重发一次必然撞上同一个冲突的写入。
  //
  // 所以 action 一律 null：只解释，不给按钮。这不是「暂时的故障」，重试不是恢复路径；
  // 恢复路径是看当前那一版行程。工作台面的 409 已有自动重同步（bundleConflict.ts），
  // 走到这张失败卡的是没有自动重同步的那些路径。
  if (/\b409\b|conflict|revision[ _-]?conflict|out[ _-]?of[ _-]?date/i.test(message)) {
    return {
      title: '这次旅行结果刚刚更新过',
      description: '服务端已经有更新的一版行程，为了不覆盖它，这次操作没有执行。请查看当前的行程结果；如果这次修改仍然需要，请在最新结果上重新发起。',
      action: null,
    };
  }

  if (/delivery[ _-]?integrity|delivery bundle|bundle (?:schema|revision|persistence)|atomic (?:save|persist|finaliz)/i.test(message)) {
    return {
      title: '旅行结果需要重新加载',
      description: '当前旅行结果未能安全保存。请刷新页面后重新载入；你的需求不会因此丢失。',
      action: 'reload',
      actionLabel: '重新加载',
    };
  }

  if (/\b5\d{2}\b|internal server error|bad gateway|service unavailable/i.test(message)) {
    return {
      title: '旅行助手暂时不可用',
      description: '你的需求已经保留，稍等片刻后可以再试一次。',
      action: 'retry',
      actionLabel: '再试一次',
    };
  }

  return {
    title: '暂时无法继续这次旅行',
    description: '可以再试一次；如果连续出现，请刷新页面后重新开始。',
    action: 'retry',
    actionLabel: '再试一次',
  };
}

export function cleanChatErrorDetail(raw: string): string {
  return normalizedError(raw) || 'Unknown error';
}

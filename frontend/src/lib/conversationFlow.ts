import type { Message } from '../types/chat';

export const RESEARCH_START_TEXT = '开始调研';

export function messageText(message?: Pick<Message, 'displayContent' | 'content'> | null): string {
  return (message?.displayContent || message?.content || '').trim();
}

export function isResearchStartMessage(message: Message): boolean {
  return message.role === 'user' && messageText(message) === RESEARCH_START_TEXT;
}

export function researchStartIndex(messages: Message[]): number {
  return messages.findIndex(isResearchStartMessage);
}

export function hasResearchStarted(messages: Message[]): boolean {
  return researchStartIndex(messages) >= 0;
}

/**
 * Everything up to and including ``boundary`` is setup, and stays out of the thread.
 *
 * A compaction is the one exception: it is a durable system event rather than
 * setup copy, so a restored thread does not silently lose its history boundary.
 */
function afterSetupBoundary(messages: Message[], boundary: number): Message[] {
  const retainedEvents = messages
    .slice(0, boundary + 1)
    .filter((message) => message.type === 'context_compaction');
  return [...retainedEvents, ...messages.slice(boundary + 1)];
}

/**
 * The chat thread, with the setup exchange projected out.
 *
 * Two boundaries, the same rule. After the user approves the plan,
 * ``RESEARCH_START_TEXT`` marks it. Before that — while the plan gate is waiting
 * for a decision — **the gate itself is the boundary** : the request that
 * raised it is answered by the gate card sitting directly below, so leaving the
 * bubble in place puts the same trip on screen three times over (the boarding
 * pass already prints origin, destination, dates, party and style; the bubble
 * repeats them as a sentence; the gate then lists the tasks derived from them).
 *
 * The gate has to be *pending* for this. A cancelled gate is kept on screen for
 * reference and the composer comes back, so the thread goes back to being a
 * conversation the traveller can read and re-send from.
 */
export function projectVisibleMessages(
  messages: Message[],
  options?: { planGatePending?: boolean },
): Message[] {
  // 用户点「载入更早的对话」翻回来的历史不参与投影：它是被明确要求看的，而 setup
  // 边界要隐藏的只是**本次运行**开始之前的那段来回。不分开的话边界落在同一条
  // `开始调研` 上，前插进来的整页历史被一并切掉 —— 按钮点了没有任何反应。
  const firstCurrent = messages.findIndex((message) => !message.isEarlierHistory);
  if (firstCurrent > 0) {
    return [
      ...messages.slice(0, firstCurrent),
      ...projectVisibleMessages(messages.slice(firstCurrent), options),
    ];
  }
  const startIndex = researchStartIndex(messages);
  if (startIndex >= 0) return afterSetupBoundary(messages, startIndex);
  if (options?.planGatePending) {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === 'user') return afterSetupBoundary(messages, index);
    }
  }
  return messages;
}

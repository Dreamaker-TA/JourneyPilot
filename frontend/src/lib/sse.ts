/**
 * SSE 流式客户端。
 *
 * streamChat 向后端 `POST /api/chat-stream` 发起请求，逐帧解析 text/event-stream
 * 响应并把每个 SSE 事件交给 onEvent。
 *
 * 重试只在握手 / 开流前的瞬时网络失败上做，有限次退避；一旦已收到任意 SSE 事件就**不再重
 * POST**（chat-stream 非幂等），结果态交给 durable recovery 承接。
 */
import type { ChatRequest } from '../types/api';
import type { SSEEvent } from '../types/chat';
import { getApiBaseUrl } from './runtimeConfig';

const HANDSHAKE_MAX_ATTEMPTS = 3;
const HANDSHAKE_BASE_DELAY_MS = 400;

function formatNetworkError(error: unknown): Error {
  if (error instanceof Error) {
    const message = error.message || '';
    if (
      error instanceof TypeError &&
      /failed to fetch|networkerror|load failed/i.test(message)
    ) {
      return new Error('无法连接到后端服务，请确认 API 服务正在运行后重试。');
    }
    return error;
  }
  return new Error(String(error));
}

function isRetryableHandshakeError(error: unknown): boolean {
  if (error instanceof TypeError) {
    return /failed to fetch|networkerror|load failed/i.test(error.message || '');
  }
  if (error instanceof Error) {
    // Transient HTTP on open; do not retry 4xx validation.
    const match = /^(\d{3})\b/.exec(error.message);
    if (match) {
      const code = Number(match[1]);
      return code >= 500 || code === 408 || code === 429;
    }
  }
  return false;
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function dispatchSseFrame(frame: string, onEvent: (event: SSEEvent) => void): void {
  const data = frame
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
    .join('\n')
    .trim();
  if (!data || data === '[DONE]') return;
  try {
    onEvent(JSON.parse(data) as SSEEvent);
  } catch (err) {
    const preview = data.length > 240 ? `${data.slice(0, 240)}…` : data;
    throw new Error(`SSE 事件解析失败：${err instanceof Error ? err.message : String(err)}；frame=${preview}`);
  }
}

async function openChatStream(
  request: ChatRequest,
  signal?: AbortSignal
): Promise<ReadableStreamDefaultReader<Uint8Array>> {
  const response = await fetch(`${getApiBaseUrl()}/chat-stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(request),
    signal,
  });

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      // 状态码必须留在原文里：它是这条错误唯一可机读的部分，而 detail 是一句中文散文。
      // 用 detail 整句替换掉 message，「409」就在边界上没了，`chatErrorGuidance` 的 409 判据
      // 再也匹配不到业务 409 —— 它们会全落到终态兜底，拿到一个误导性的「再试一次」。
      //
      // 原文只进判据与检查面（MessageBubble 的圆圈 i）；用户正文读的是
      // getChatErrorGuidance 投影出来的 title/description，所以带上状态码不会让
      // 普通用户看到 HTTP 词汇。
      const detail: unknown = body?.detail;
      const parts = [String(response.status)];
      if (typeof detail === 'string' && detail.trim()) {
        parts.push(detail.trim());
      } else if (detail && typeof detail === 'object') {
        const record = detail as Record<string, unknown>;
        if (typeof record.code === 'string' && record.code) parts.push(record.code);
        if (typeof record.message === 'string' && record.message) parts.push(record.message);
      }
      if (parts.length > 1) message = parts.join(' ');
    } catch {
      // keep HTTP status fallback
    }
    throw new Error(message);
  }
  if (!response.body) throw new Error('SSE 响应体为空');
  return response.body.getReader();
}

export async function streamChat(
  request: ChatRequest,
  onEvent: (event: SSEEvent) => void,
  onError: (error: Error) => void,
  signal?: AbortSignal
): Promise<void> {
  try {
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    let lastError: unknown = null;

    for (let attempt = 1; attempt <= HANDSHAKE_MAX_ATTEMPTS; attempt += 1) {
      if (signal?.aborted) return;
      try {
        reader = await openChatStream(request, signal);
        lastError = null;
        break;
      } catch (e) {
        lastError = e;
        if (signal?.aborted) return;
        if (!isRetryableHandshakeError(e) || attempt >= HANDSHAKE_MAX_ATTEMPTS) {
          throw e;
        }
        await wait(HANDSHAKE_BASE_DELAY_MS * 2 ** (attempt - 1));
      }
    }

    if (!reader) {
      throw lastError instanceof Error ? lastError : new Error(String(lastError));
    }

    const decoder = new TextDecoder();
    let buffer = '';
    let receivedAnyEvent = false;

    const wrappedOnEvent = (event: SSEEvent) => {
      receivedAnyEvent = true;
      onEvent(event);
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || '';
      for (const frame of frames) {
        dispatchSseFrame(frame, wrappedOnEvent);
      }
    }

    buffer += decoder.decode();
    if (buffer.trim()) dispatchSseFrame(buffer, wrappedOnEvent);

    // Mid-stream disconnects are not re-POSTed (non-idempotent). Callers rely on
    // delivery event recovery for durable results after partial process streams.
    void receivedAnyEvent;
  } catch (e) {
    if (signal?.aborted) return;
    onError(formatNetworkError(e));
  }
}

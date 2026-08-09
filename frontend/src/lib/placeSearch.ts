/**
 * 地点搜索的「只有最新一次请求能写状态」守卫。
 *
 * 意图表单是输入即搜索：每一次按键都可能发出一次请求，多个请求同时在飞，谁先回来不由发起
 * 顺序决定。守卫必须盖住**三样**：候选列表、`error`、`loading`。少守 `error` / `loading`，
 * 一次慢的失败响应回来时就会写下一条属于旧输入的错误，并把新请求正在进行中的「搜索中…」
 * 清成 false —— 用户看到一条对当前输入不成立的报错，加上一个已经不转了的输入框。
 *
 * 搜索逻辑放在这里而不留在组件里，是为了让这条不变量可以被独立驱动 —— 跟组件
 * 内联闭包解耦，不变量是纯的、可单测的。
 */
import type { PlaceIdentity } from '../types/api';
import { api } from './api';
import { describeRequestFailure } from './requestFailureMessage';

/**
 * 这次请求自己的墙钟。
 *
 * 与 `api.ts` 的 `SLOW_TIMEOUT_MS` 同档（搜索 30s）。之所以由这里计时而不是让
 * `fetchJson` 套 `AbortSignal.timeout`：调用方传了 `signal` 时 api 层不叠加超时
 * （见 `requestSignal`），所以自带 signal 的一方必须自带时钟，否则这条请求就是
 * 唯一一条没有墙钟的请求。
 */
export const PLACE_SEARCH_TIMEOUT_MS = 30_000;

const EMPTY_RESULT_MESSAGE: Record<PlaceSearchRole, string> = {
  origin: '没有找到可用的城市、机场或火车站',
  destination: '请选择城市、行政区域、岛屿或景区型区域',
};

export type PlaceSearchRole = 'origin' | 'destination';

export interface PlaceSearchCandidate {
  place: PlaceIdentity;
  confidence: number;
}

/** 组件侧的三个 setState。守卫决定它们哪一次允许被调用。 */
export interface PlaceSearchSink {
  setLoading(value: boolean): void;
  setError(message: string): void;
  setCandidates(candidates: PlaceSearchCandidate[]): void;
}

type PlaceSearchFetcher = (
  query: string,
  role: PlaceSearchRole,
  options: { signal: AbortSignal }
) => Promise<{ candidates: PlaceSearchCandidate[] }>;

export interface PlaceSearchRunner {
  run(query: string, sink: PlaceSearchSink): Promise<void>;
  /**
   * 输入被清空或组件卸载：掐掉在飞的请求，并让它此后写不进任何状态。
   * 不负责收 loading——调用它的那条路径本来就要把字段整体复位。
   */
  cancel(): void;
}

function isAbort(reason: unknown): boolean {
  return (reason as { name?: string } | null)?.name === 'AbortError';
}

export function createPlaceSearchRunner(
  role: PlaceSearchRole,
  fetcher: PlaceSearchFetcher = api.searchPlaces
): PlaceSearchRunner {
  let latest = 0;
  let inflight: AbortController | null = null;
  let clock: ReturnType<typeof setTimeout> | null = null;

  function stopClock() {
    if (clock !== null) {
      clearTimeout(clock);
      clock = null;
    }
  }

  function cancel() {
    inflight?.abort();
    inflight = null;
    stopClock();
    // 递增 ticket：在飞的那次请求回来时已经不是 latest，写不进任何状态。
    latest += 1;
  }

  return {
    cancel,
    async run(query, sink) {
      // 新请求一发出，旧请求就没有写状态的资格了——先掐掉，别占着连接、也别等它
      // 回来才发现自己过期。
      inflight?.abort();
      stopClock();

      const ticket = ++latest;
      const isCurrent = () => ticket === latest;
      const controller = new AbortController();
      inflight = controller;
      let timedOut = false;
      clock = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, PLACE_SEARCH_TIMEOUT_MS);

      sink.setLoading(true);
      sink.setError('');
      try {
        const result = await fetcher(query, role, { signal: controller.signal });
        if (!isCurrent()) return;
        sink.setCandidates(result.candidates);
        if (!result.candidates.length) sink.setError(EMPTY_RESULT_MESSAGE[role]);
      } catch (reason) {
        if (!isCurrent()) return;
        if (timedOut) {
          sink.setError('地点搜索超时，请重试');
          return;
        }
        // 自己取消的请求不是失败：静默退出，否则用户会读到一条由自己的下一次
        // 输入引发的「错误」。
        if (isAbort(reason)) return;
        // 后端原文不进屏幕。
        sink.setError(describeRequestFailure(reason, '读取', '地点候选').message);
      } finally {
        // loading 与 error 同样要守卫：过期响应无论成功还是失败，都不得
        // 把新请求正在进行中的 loading 清掉。
        if (isCurrent()) {
          stopClock();
          inflight = null;
          sink.setLoading(false);
        }
      }
    },
  };
}

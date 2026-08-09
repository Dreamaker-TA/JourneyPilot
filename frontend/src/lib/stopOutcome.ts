import { apiErrorDetail, apiErrorDetailString } from './apiErrorDetail';
import { isTripRunStatus } from '../types/api';
import type { TripRunStatus } from '../types/api';

export interface StopOutcome {
  /** 本次运行确实停下来了（由用户这次操作，或已经由别的原因结束）。 */
  stopped: boolean;
  /** 服务端报告的当前状态；`null` 表示这次响应没说。 */
  status: TripRunStatus | null;
  /** 要显示给用户的一句话；`null` 表示没有额外要说的。 */
  message: string | null;
}

/**
 * 停止请求失败时，到底发生了什么。
 *
 * 「来晚了」和「没停下来」是**两件事**，不能共用一句话。运行已经结束时服务端会答 409
 * `run_not_cancellable` 并带上当前状态；把它说成「后端取消未确认」等于告诉用户还有个东西
 * 在跑 —— 而实际上没有，用户还会去点第二次。
 */
export function stopOutcomeFromError(error: unknown): StopOutcome {
  const detail = apiErrorDetail(error);
  if (detail.code === 'run_not_cancellable') {
    const reported = apiErrorDetailString(error, 'status');
    const status = isTripRunStatus(reported) ? reported : null;
    return {
      stopped: true,
      status,
      message: status === 'cancelled' ? null : '这次运行已经结束了，不需要再停止。',
    };
  }
  // 后端原文不进这句话：`detail.message` / `error.message` 是 `${status} ${statusText}`
  // 或 pydantic 的 `loc: msg` 串，合同 §UX Copy 明令普通模式不许出现。「本地流」「后端」
  // 也是内部说法——旅行者要知道的是「这一屏停了、那边可能还在跑」。
  return {
    stopped: false,
    status: null,
    message: '这个页面已经停止接收结果，但规划可能仍在继续。稍后回到这趟旅行看它的状态。',
  };
}

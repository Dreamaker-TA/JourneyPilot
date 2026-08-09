import { ApiError } from './api';

/**
 * 一次数据面读写失败，对旅行者说的那一句话 —— **以及那一句话之后他能按的那个键**。
 *
 * 为什么不复用 `getChatErrorGuidance`：那一份**只有一段文本可读**（SSE 失败到达时是一串字，
 * 没有状态码），所以它按正则猜；它答的也是另一个问题——「这次对话怎么恢复」，要给出
 * title / description / 一个恢复动作。这一份走的是 HTTP 客户端那条路，`ApiError.status`
 * 是**结构化的**，不必猜；它答的问题只有一个：「这一屏刚才没读到东西，怎么跟人说」。
 * 一个读结构、一个读字符串，不是同一件事的两份实现。
 *
 * 为什么需要它：`api.ts` 的 `ApiError.message` 兜底是 `` `${status} ${statusText}` ``，
 * 422 时是 `formatApiErrorDetail` 拼出来的 `body.user_id: field required` 这种 loc: msg 串。
 * 这些串**一个字都不许印给旅行者** —— 「Normal client mode never
 * prints raw HTTP status text, exception classes, stack traces, or backend messages」，而
 * 合同给的唯一出口（developerMode 折叠区）本产品不做，所以这些技术文本没有归宿。
 *
 * 为什么返回的不只是一句话：合同 §UX Copy 还写着「Only offer retry when repeating the
 * request can plausibly recover. Route mismatches such as 404/405 guide the user to restore
 * the JourneyPilot service and refresh」。**能不能重试和句子同源**，所以一起返回，界面不各自
 * 猜 —— 各自猜的结果是有的屏无条件印「重试」（404 时按一百次也是 404），有的屏一个键都不给
 * 而句子却写着「请稍后重试」。
 */

/** 这一屏刚才在做什么。用词进句子，所以是动词。 */
export type RequestAction = '读取' | '上传' | '添加' | '删除' | '保存' | '重命名';

/**
 * 重复这次请求能不能恢复。
 *
 * - `retry`：能。服务暂时不可用、限流、网络断——同一个请求过一会儿可能就成了。
 * - `reload`：不能，但整页重来可能。路由对不上（404/405，通常是后端没起或版本不对）、
 *   请求信息不完整（400/422，前端状态已经脏了）——这两种按「重试」是在原地打转。
 * - `none`：不能，而且不是这一屏能解决的（403 权限）。不给键比给一个按了没用的键诚实。
 */
export type RequestRecovery = 'retry' | 'reload' | 'none';

export interface RequestFailure {
  /** 印给旅行者的那一句。永不含状态码、异常类名、后端原文。 */
  message: string;
  recovery: RequestRecovery;
}

/**
 * `subject` 是这一屏正在操作的东西，进句子当宾语（「资料库状态」「旅行风格」「我的偏好」）。
 * 它由屏幕自己给，因为只有屏幕知道刚才读的是什么——泛化成「数据」就又变成一句空话。
 */
export function describeRequestFailure(
  error: unknown,
  action: RequestAction,
  subject: string,
): RequestFailure {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return { message: `当前用户无法${action}${subject}。`, recovery: 'none' };
    }
    if (error.status === 400 || error.status === 422) {
      return { message: `${action}请求的信息不完整，请刷新页面后重试。`, recovery: 'reload' };
    }
    if (error.status === 404 || error.status === 405) {
      return {
        message: `找不到${subject}的服务地址，请确认 JourneyPilot 正在运行，然后刷新页面。`,
        recovery: 'reload',
      };
    }
    if (error.status === 429) {
      return { message: `请求有点多，稍等片刻再${action}${subject}。`, recovery: 'retry' };
    }
    if (error.status >= 500) {
      return { message: `服务暂时无法${action}${subject}，请稍后重试。`, recovery: 'retry' };
    }
    // 上面那张表没列到的状态码 —— 服务端**答了**，只是答的这一档这里还没有专门的一句话。
    // 它**不是**「连不上服务」：这两件事此前合并在最后那一句「请检查网络后重试」上，
    // 于是一份 11 MB 的文件（413）与一次版本冲突（409）都被说成用户的网络有问题，
    // 而重试同一个请求对这两种都没用（短的那张表有兜底，兜底把「我不认识
    // 这一档」画成「你的网络断了」）。这一支按 4xx 的共性说话：请求本身被拒了，
    // 重发没用，整页重来才可能拿到一个不会被拒的状态。
    return {
      message: `服务端没有接受这次${action}，请刷新页面后再试。`,
      recovery: 'reload',
    };
  }
  // 这里**只**剩下真的连不上：fetch 抛错、超时、跨域被拦 —— 没有状态码可读。
  return {
    message: `无法连接服务，${action}不到${subject}，请检查网络后重试。`,
    recovery: 'retry',
  };
}

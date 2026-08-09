/**
 * REST API 客户端。
 *
 * 通过 VITE_API_BASE 指向真实 FastAPI 后端（默认 /api）。
 */
import type {
  KnowledgeCollectionStats,
  KnowledgeDeleteResponse,
  KnowledgeSourceDocument,
  KnowledgeUploadResponse,
  AddMemoryFactResponse,
  MemoryDeleteAllOptions,
  MemoryDeleteOptions,
  MemoryDeletionResponse,
  MemoryFactListResponse,
  MemoryRetentionCleanupRequest,
  ModelConfigRequest,
  SessionDetail,
  SessionSummary,
  SystemConfig,
  ToolInfo,
  TripRunControlRequest,
  TripRunControlResponse,
  TripRunDetailResponse,
  TripRunEventWindowResponse,
  TripRunListResponse,
  UserProfile,
  PlaceIdentity,
  PreferenceOptionGroup,
  TripPlannerConfiguration,
} from '../types/api';
import type {
  GenerateInstructionsResult,
  PresetCreateData,
  PresetUpdateData,
  TravelPreset,
} from '../types/preset';
import type {
  PublicDeliveryBundle,
  WorkspaceV2MutationPreviewResponse,
  WorkspaceV2MutationRequest,
  WorkspaceV2MutationResponse,
  WorkspaceV2UndoHead,
  WorkspaceV2UndoRequest,
  WorkspaceV2UndoResponse,
  WeatherBundleRefreshRequest,
  WeatherBundleRefreshResponse,
} from '../types/delivery';
import { getApiBaseUrl } from './runtimeConfig';

function withQuery(path: string, params: Record<string, string | number | null | undefined>): string {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') {
      query.set(key, String(value));
    }
  });
  const qs = query.toString();
  return qs ? `${path}?${qs}` : path;
}

function requireResolvedUserId(userId: string): string {
  const resolved = userId.trim();
  if (!resolved || resolved === 'anonymous') {
    throw new Error('用户身份尚未就绪');
  }
  return resolved;
}

function formatApiErrorDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (!item || typeof item !== 'object') return String(item);
      const row = item as Record<string, unknown>;
      const loc = Array.isArray(row.loc) ? row.loc.join('.') : '';
      const msg = typeof row.msg === 'string' ? row.msg : JSON.stringify(row);
      return loc ? `${loc}: ${msg}` : msg;
    });
    return parts.filter(Boolean).join('; ') || null;
  }
  if (detail && typeof detail === 'object') {
    // 机读 detail 的既有形状是 `{code, message}`。`message` 是给人看的那一半，
    // 直接 JSON.stringify 会把 `{"code":"place_provider_unavailable",…}` 原样
    // 显示到输入框下面。只带 code 的站点（如 report_out_of_date）另有专门的
    // 前端分支读 `body.detail.code`，走不到这里的展示位。
    const record = detail as Record<string, unknown>;
    if (typeof record.message === 'string' && record.message.trim()) return record.message;
    return JSON.stringify(detail);
  }
  return null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * 每个请求的墙钟上限。
 *
 * 裸 `fetch` 没有超时——上游卡住时 promise 永不 settle，调用方的 loading 就永远转，
 * 用户没有任何反馈也没有重试入口。下面四档按「后端最坏路径的量级」给，不是随手放宽：
 * 慢是因为对端真的在做重活（LLM、文档解析、PDF 渲染），而不是因为它挂了。
 */
const DEFAULT_TIMEOUT_MS = 15_000;
/** 搜索与工作台 mutation：要打外部地点服务，或要重算并原子落一版 Bundle。 */
const SLOW_TIMEOUT_MS = 30_000;
/** 对端是一次 LLM 调用。 */
const LLM_TIMEOUT_MS = 60_000;
/** 对端要解析上传文档或渲染整本 PDF。 */
const DOCUMENT_TIMEOUT_MS = 120_000;

/**
 * 调用方自带 `signal` 时**不叠加**超时。
 *
 * 自带 signal 意味着调用方已经在管这次请求的生命周期（取消、被新请求取代、组件卸载），
 * 它自己就该带上墙钟；再套一层这里看不见的 `AbortSignal.timeout` 只会让「谁掐的」
 * 变成猜谜。参见 `placeSearch.ts`——它自带 30s 时钟，正是为了填这个位置。
 */
function requestSignal(init: RequestInit | undefined, timeoutMs: number): AbortSignal {
  return init?.signal ?? AbortSignal.timeout(timeoutMs);
}

/**
 * `AbortSignal.timeout` 抛的是浏览器内置英文 message 的 `TimeoutError`，而调用方
 * 普遍直接把 `error.message` 显示给用户。这里换成可读的中文。
 *
 * 调用方自己 abort 的 `AbortError` 不碰：它必须原样穿透，好让请求守卫认出
 * 「这是我自己取消的，不是失败」。
 */
function readableTransportError(reason: unknown, timeoutMs: number): unknown {
  if ((reason as { name?: string } | null)?.name === 'TimeoutError') {
    return new Error(`请求超时（${Math.round(timeoutMs / 1000)} 秒未响应）`);
  }
  return reason;
}

async function fetchJson<T>(
  path: string,
  init?: RequestInit,
  timeoutMs: number = DEFAULT_TIMEOUT_MS
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  let response: Response;
  try {
    response = await fetch(`${getApiBaseUrl()}${path}`, {
      ...init,
      headers,
      signal: requestSignal(init, timeoutMs),
    });
  } catch (reason) {
    throw readableTransportError(reason, timeoutMs);
  }

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    let body: unknown = null;
    try {
      body = await response.json();
      const record = body && typeof body === 'object' ? body as Record<string, unknown> : null;
      message = formatApiErrorDetail(record?.detail)
        || (typeof record?.message === 'string' ? record.message : message);
    } catch {
      // keep HTTP status fallback
    }
    throw new ApiError(message, response.status, body);
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

async function fetchPdf(path: string, init: RequestInit, timeoutMs: number = DEFAULT_TIMEOUT_MS): Promise<{
  blob: Blob;
  filename: string;
  bundleId: string;
}> {
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  let response: Response;
  try {
    response = await fetch(`${getApiBaseUrl()}${path}`, {
      ...init,
      headers,
      signal: requestSignal(init, timeoutMs),
    });
  } catch (reason) {
    throw readableTransportError(reason, timeoutMs);
  }
  if (!response.ok) {
    let body: unknown = null;
    let message = `${response.status} ${response.statusText}`;
    try {
      body = await response.json();
      const record = body && typeof body === 'object' ? body as Record<string, unknown> : null;
      message = formatApiErrorDetail(record?.detail)
        || (typeof record?.message === 'string' ? record.message : message);
    } catch {
      // keep HTTP status fallback
    }
    throw new ApiError(message, response.status, body);
  }
  const disposition = response.headers.get('content-disposition') ?? '';
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? 'journeypilot-trip.pdf';
  return {
    blob: await response.blob(),
    filename,
    bundleId: response.headers.get('x-journeypilot-bundle-id') ?? '',
  };
}

export const api = {
  async getTripPlannerConfiguration(): Promise<TripPlannerConfiguration> {
    return fetchJson<TripPlannerConfiguration>('/product/trip-planner');
  },

  async searchPlaces(
    query: string,
    role: 'origin' | 'destination' | 'itinerary_place',
    options?: { signal?: AbortSignal }
  ): Promise<{ query: string; role: string; candidates: Array<{ place: PlaceIdentity; confidence: number; requires_confirmation: boolean }> }> {
    // 传了 signal 就由调用方自带墙钟（见 requestSignal）：`placeSearch.ts` 的
    // 最新请求守卫按同一个 30s 档位计时。
    return fetchJson(withQuery('/places/search', { q: query, role }), { signal: options?.signal }, SLOW_TIMEOUT_MS);
  },

  async getDefaultOrigin(userId: string): Promise<PlaceIdentity | null> {
    const result = await fetchJson<{ user_id: string; place: PlaceIdentity | null }>(`/users/${encodeURIComponent(userId)}/default-origin`);
    return result.place;
  },

  async setDefaultOrigin(userId: string, place: PlaceIdentity): Promise<PlaceIdentity> {
    const result = await fetchJson<{ user_id: string; place: PlaceIdentity }>(`/users/${encodeURIComponent(userId)}/default-origin`, {
      method: 'PUT', body: JSON.stringify({ place }),
    });
    return result.place;
  },
  async controlTripRun(runId: string, request: TripRunControlRequest): Promise<TripRunControlResponse> {
    return fetchJson<TripRunControlResponse>(
      `/trip-runs/${encodeURIComponent(runId)}/control`,
      {
        method: 'POST',
        body: JSON.stringify(request),
      }
    );
  },

  async addTripRunSupplement(runId: string, request: { category: import('../types/api').TripSupplementCategory; content: string; user_id: string; session_id?: string | null }): Promise<import('../types/api').TripRunSupplementResponse> {
    return fetchJson(`/trip-runs/${encodeURIComponent(runId)}/supplements`, {
      method: 'POST',
      body: JSON.stringify(request),
    });
  },

  async listTripRuns(userId: string, options?: { sessionId?: string; mode?: 'deep' | 'fast'; limit?: number }): Promise<TripRunListResponse> {
    return fetchJson<TripRunListResponse>(
      withQuery('/trip-runs', {
        user_id: userId,
        session_id: options?.sessionId,
        mode: options?.mode,
        limit: options?.limit ?? 80,
      })
    );
  },

  async getTripRunDetail(
    runId: string,
    userId: string,
    options?: { sessionId?: string | null }
  ): Promise<TripRunDetailResponse> {
    return fetchJson<TripRunDetailResponse>(
      withQuery(`/trip-runs/${encodeURIComponent(runId)}`, {
        event_limit: 80,
        user_id: userId,
        session_id: options?.sessionId,
      })
    );
  },

  async getTripRunEventWindow(
    runId: string,
    userId: string,
    options: {
      sessionId?: string | null;
      afterSequence: number;
      limit?: number;
    }
  ): Promise<TripRunEventWindowResponse> {
    return fetchJson<TripRunEventWindowResponse>(
      withQuery(`/trip-runs/${encodeURIComponent(runId)}/events`, {
        user_id: userId,
        session_id: options.sessionId,
        after_sequence: options.afterSequence,
        limit: options.limit ?? 200,
      })
    );
  },

  async getCurrentDeliveryBundle(runId: string, userId: string, sessionId?: string | null): Promise<PublicDeliveryBundle> {
    return fetchJson<PublicDeliveryBundle>(
      withQuery(`/trip-runs/${encodeURIComponent(runId)}/bundle/current`, {
        user_id: userId,
        session_id: sessionId,
      })
    );
  },


  async exportCurrentTripReportPdf(
    bundle: PublicDeliveryBundle,
    userId: string,
    sessionId?: string | null
  ): Promise<{ blob: Blob; filename: string; bundleId: string }> {
    const manifest = bundle.manifest;
    return fetchPdf(
      `/trip-runs/${encodeURIComponent(manifest.run_id)}/bundle/current/pdf`,
      {
        method: 'POST',
        body: JSON.stringify({
          user_id: userId,
          session_id: sessionId,
          bundle_id: manifest.bundle_id,
          workspace_revision: manifest.workspace_revision,
          fact_data_revision: manifest.fact_data_revision,
          weather_data_revision: manifest.weather_data_revision,
        }),
      },
      DOCUMENT_TIMEOUT_MS
    );
  },

  async refreshCurrentBundleWeather(
    runId: string,
    request: WeatherBundleRefreshRequest
  ): Promise<WeatherBundleRefreshResponse> {
    return fetchJson<WeatherBundleRefreshResponse>(
      `/trip-runs/${encodeURIComponent(runId)}/bundle/current/weather/refresh`,
      { method: 'POST', body: JSON.stringify(request) },
      LLM_TIMEOUT_MS
    );
  },

  async previewWorkspaceV2Mutation(
    runId: string,
    request: WorkspaceV2MutationRequest
  ): Promise<WorkspaceV2MutationPreviewResponse> {
    return fetchJson<WorkspaceV2MutationPreviewResponse>(
      `/trip-runs/${encodeURIComponent(runId)}/workspace/mutations/preview`,
      { method: 'POST', body: JSON.stringify(request) },
      SLOW_TIMEOUT_MS
    );
  },

  async applyWorkspaceV2Mutation(
    runId: string,
    request: WorkspaceV2MutationRequest
  ): Promise<WorkspaceV2MutationResponse> {
    return fetchJson<WorkspaceV2MutationResponse>(
      `/trip-runs/${encodeURIComponent(runId)}/workspace/mutations`,
      { method: 'POST', body: JSON.stringify(request) },
      SLOW_TIMEOUT_MS
    );
  },

  async getWorkspaceV2Mutation(
    runId: string,
    mutationId: string,
    userId: string,
    sessionId?: string | null
  ): Promise<WorkspaceV2MutationResponse> {
    return fetchJson<WorkspaceV2MutationResponse>(
      withQuery(
        `/trip-runs/${encodeURIComponent(runId)}/workspace/mutations/${encodeURIComponent(mutationId)}`,
        { user_id: userId, session_id: sessionId }
      )
    );
  },

  async getWorkspaceV2UndoHead(
    runId: string,
    userId: string,
    sessionId?: string | null
  ): Promise<WorkspaceV2UndoHead> {
    return fetchJson<WorkspaceV2UndoHead>(
      withQuery(`/trip-runs/${encodeURIComponent(runId)}/workspace/undo-head`, {
        user_id: userId,
        session_id: sessionId,
      })
    );
  },

  async undoWorkspaceV2Mutation(
    runId: string,
    request: WorkspaceV2UndoRequest
  ): Promise<WorkspaceV2UndoResponse> {
    return fetchJson<WorkspaceV2UndoResponse>(
      `/trip-runs/${encodeURIComponent(runId)}/workspace/undo`,
      { method: 'POST', body: JSON.stringify(request) },
      SLOW_TIMEOUT_MS
    );
  },

  async listSessions(userId: string): Promise<SessionSummary[]> {
    return fetchJson<SessionSummary[]>(`/users/${encodeURIComponent(userId)}/sessions`);
  },

  async getSessionDetail(userId: string, sessionId: string): Promise<SessionDetail> {
    return fetchJson<SessionDetail>(
      `/users/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}`
    );
  },

  async deleteSession(userId: string, sessionId: string): Promise<void> {
    await fetchJson<{ status?: string }>(
      `/users/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}`,
      { method: 'DELETE' }
    );
  },

  async renameSession(userId: string, sessionId: string, title: string): Promise<SessionSummary> {
    return fetchJson<SessionSummary>(
      `/users/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}`,
      { method: 'PATCH', body: JSON.stringify({ title }) }
    );
  },

  async compactSession(sessionId: string, userId: string) {
    return fetchJson<import('../types/api').ContextCompactionPayload>(`/sessions/${encodeURIComponent(sessionId)}/compact`, {
      method: 'POST',
      body: JSON.stringify({ user_id: userId }),
    }, LLM_TIMEOUT_MS);
  },

  async optimizePrompt(text: string) {
    return fetchJson<{
      success: boolean;
      optimized_prompt?: string;
      error_message?: string;
    }>('/optimize-prompt', {
      method: 'POST',
      body: JSON.stringify({ prompt: text }),
    }, LLM_TIMEOUT_MS);
  },

  async getConfig(): Promise<SystemConfig> {
    return fetchJson<SystemConfig>('/config');
  },

  async updateConfig(req: ModelConfigRequest): Promise<{ message: string }> {
    const result = await fetchJson<{ status?: string; message?: string }>('/configure', {
      method: 'POST',
      body: JSON.stringify(req),
    });
    return { message: result.message || '配置已更新' };
  },

  async getTools(): Promise<{ tools: ToolInfo[]; total: number }> {
    return fetchJson<{ tools: ToolInfo[]; total: number }>('/tools');
  },

  async getUserProfile(userId: string): Promise<UserProfile> {
    const profile = await fetchJson<Partial<UserProfile>>(`/users/${encodeURIComponent(userId)}/profile`);
    return {
      user_id: profile.user_id || userId,
      display_name: profile.display_name || '',
      preferences: profile.preferences || {},
      trip_history_count: profile.trip_history_count || profile.trip_history?.length || 0,
      trip_history: profile.trip_history || [],
    };
  },

  /**
   * 六组偏好的选项表。**这一屏画 chip 用的唯一来源** —— 不许在前端另写一份
   * （后端 `entities/user.py::TRAVEL_PREFERENCE_GROUPS` 是定义处）。
   *
   * 路径上没有 user_id：这张表与人无关。
   */
  async getPreferenceOptions(): Promise<PreferenceOptionGroup[]> {
    return fetchJson<PreferenceOptionGroup[]>('/users/preference-options');
  },

  async updatePreferences(userId: string, preferences: Record<string, unknown>): Promise<void> {
    await fetchJson<{ status?: string }>(`/users/${encodeURIComponent(userId)}/preferences`, {
      method: 'PATCH',
      body: JSON.stringify({ preferences }),
    });
  },

  async deleteMemoryFact(userId: string, factId: string, options: MemoryDeleteOptions = {}): Promise<MemoryDeletionResponse> {
    return fetchJson<MemoryDeletionResponse>(
      `/users/${encodeURIComponent(userId)}/memory/facts/${encodeURIComponent(factId)}`,
      {
        method: 'DELETE',
        body: JSON.stringify(options),
      }
    );
  },

  async deleteAllMemory(userId: string, options: MemoryDeleteAllOptions = {}): Promise<MemoryDeletionResponse> {
    return fetchJson<MemoryDeletionResponse>(`/users/${encodeURIComponent(userId)}/memory`, {
      method: 'DELETE',
      body: JSON.stringify(options),
    });
  },

  async cleanupExpiredMemory(userId: string, request: MemoryRetentionCleanupRequest = {}): Promise<MemoryDeletionResponse> {
    return fetchJson<MemoryDeletionResponse>(`/users/${encodeURIComponent(userId)}/memory/retention/cleanup`, {
      method: 'POST',
      body: JSON.stringify(request),
    });
  },

  async listMemoryFacts(userId: string): Promise<MemoryFactListResponse> {
    return fetchJson<MemoryFactListResponse>(`/users/${encodeURIComponent(userId)}/memory/facts`);
  },

  /**
   * 界面上手动添加的一条记忆。**不带分类**：那一屏没有分类控件，用户写下的是
   * 一整句没有被归类过的话，后端会把它落成 `preference`。
   *
   * 此前这里有第三个可选参数 `category`，而唯一的调用点从不传它 —— 请求体里
   * 因此永远是 `category: null`，与不传等价。摆一个没人拨的旋钮比没有更糟：
   * 读代码的人会以为界面能给记忆分类。真做出控件时在这里把参数加回来。
   *
   * 后端那个字段本身是活的：按 constraint /
   * preference 两档真的在发，界面按它印分类标签。没有生产方的只有前端这一侧。
   */
  async addMemoryFact(userId: string, content: string): Promise<AddMemoryFactResponse> {
    return fetchJson<AddMemoryFactResponse>(`/users/${encodeURIComponent(userId)}/memory/facts`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    });
  },

  async knowledgeUploadFile(userId: string, file: File, collection: string): Promise<KnowledgeUploadResponse> {
    const resolvedUserId = requireResolvedUserId(userId);
    const form = new FormData();
    form.append('file', file);
    form.append('collection', collection);
    form.append('source', file.name);
    form.append('user_id', resolvedUserId);
    return fetchJson<KnowledgeUploadResponse>('/knowledge/upload-file', {
      method: 'POST',
      body: form,
    }, DOCUMENT_TIMEOUT_MS);
  },

  async knowledgeIndex(userId: string, content: string, collection: string, source = `manual-input-${new Date().toISOString()}`): Promise<KnowledgeUploadResponse> {
    return fetchJson<KnowledgeUploadResponse>(withQuery('/knowledge/index', {
      user_id: requireResolvedUserId(userId),
    }), {
      method: 'POST',
      body: JSON.stringify({ content, collection, source }),
    }, DOCUMENT_TIMEOUT_MS);
  },

  async knowledgeDeleteCollection(userId: string, collection: string): Promise<KnowledgeDeleteResponse> {
    return fetchJson<KnowledgeDeleteResponse>(withQuery(`/knowledge/collection/${encodeURIComponent(collection)}`, {
      user_id: requireResolvedUserId(userId),
    }), {
      method: 'DELETE',
    });
  },

  /**
   * 一篇资料的正文。来源名走查询参数（后端同理）——文件名里可以有斜杠和点，
   * 塞进路径段会被路由切开。
   */
  async getKnowledgeSource(userId: string, collection: string, source: string): Promise<KnowledgeSourceDocument> {
    return fetchJson<KnowledgeSourceDocument>(withQuery(`/knowledge/collections/${encodeURIComponent(collection)}/source`, {
      user_id: requireResolvedUserId(userId),
      source,
    }));
  },

  /**
   * 改写一篇资料。走 `DOCUMENT_TIMEOUT_MS`：保存等于重新分段入库（重新 embedding，
   * contextual 分块还逐段调一次 fast 模型），和上传一份文件一样慢，不是一次普通的
   * 表单提交。
   */
  async updateKnowledgeSource(userId: string, collection: string, source: string, content: string): Promise<KnowledgeUploadResponse> {
    return fetchJson<KnowledgeUploadResponse>(withQuery(`/knowledge/collections/${encodeURIComponent(collection)}/source`, {
      user_id: requireResolvedUserId(userId),
      source,
    }), {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }, DOCUMENT_TIMEOUT_MS);
  },

  async deleteKnowledgeSource(userId: string, collection: string, source: string): Promise<KnowledgeDeleteResponse> {
    return fetchJson<KnowledgeDeleteResponse>(withQuery(`/knowledge/collections/${encodeURIComponent(collection)}/source`, {
      user_id: requireResolvedUserId(userId),
      source,
    }), {
      method: 'DELETE',
    });
  },

  async getKnowledgeCollectionStats(userId: string, collection: string): Promise<KnowledgeCollectionStats> {
    return fetchJson<KnowledgeCollectionStats>(withQuery(`/knowledge/collections/${encodeURIComponent(collection)}/stats`, {
      user_id: requireResolvedUserId(userId),
    }));
  },

  async listPresets(userId: string): Promise<TravelPreset[]> {
    return fetchJson<TravelPreset[]>(`/presets?user_id=${encodeURIComponent(userId)}`);
  },

  async createPreset(data: PresetCreateData): Promise<TravelPreset> {
    return fetchJson<TravelPreset>('/presets', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async updatePreset(id: string, data: PresetUpdateData): Promise<TravelPreset> {
    return fetchJson<TravelPreset>(`/presets/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  },

  async deletePreset(id: string, userId: string): Promise<void> {
    await fetchJson<{ status?: string }>(`/presets/${encodeURIComponent(id)}?user_id=${encodeURIComponent(userId)}`, {
      method: 'DELETE',
    });
  },

  async generatePresetInstructions(prompt: string, userId: string): Promise<GenerateInstructionsResult> {
    return fetchJson<GenerateInstructionsResult>('/presets/generate-instructions', {
      method: 'POST',
      body: JSON.stringify({ description: prompt, user_id: userId }),
    }, LLM_TIMEOUT_MS);
  },
};

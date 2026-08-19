/**
 * Agent Timeline / 责任视图：把固定 LangGraph 工作流的节点责任边界，翻译成用户能理解的
 * 5 个责任阶段（规划 → 调研 → 校验 → 评审 → 合成）。
 *
 * - 数据走 SSE 结构化事件字段（thinkingSteps[].agentName，= 后端 agent_name），不解析自然语言。
 * - 5 阶段写死、始终全列，体现「固定责任边界」——不暗示动态生成任意 Agent 团队（JP-03-03 §8）。
 * - 只给阶段状态 + 职责 + 必要的补研原因；不展示模型内部思考 / token / 延迟 / 工具全量
 *   （那些留在现有 chat 原始 trace，本视图是另一个 altitude）。
 *
 * 阶段映射对齐后端 `src/travel_agent/utils/display_names.py` 与 LangGraph 图结构
 * （workflows/travel_planning.py）；阶段职责文案对齐 JP-02-05 工作流职责语义。
 */
import type { ThinkingStep } from '../types/chat';

export type StageId = 'planning' | 'research' | 'verify' | 'review' | 'synthesis';
export type StageStatus = 'pending' | 'active' | 'done';

export interface StageDef {
  id: StageId;
  label: string;
  /** 用户可读的职责一句话 */
  responsibility: string;
}

/** 固定 5 阶段（顺序即工作流责任边界，始终全列） */
export const STAGE_DEFS: StageDef[] = [
  { id: 'planning', label: '规划', responsibility: '理解你的需求，拆解调研计划并分派任务' },
  { id: 'research', label: '调研', responsibility: '并行调研目的地、交通、住宿，编排每日行程' },
  { id: 'verify', label: '准入', responsibility: '校验候选的约束、来源、天气与现实身份' },
  { id: 'review', label: '成行', responsibility: '验证相邻交通、行程拓扑与完整交付质量' },
  { id: 'synthesis', label: '交付', responsibility: '生成统一投影并原子保存正式旅行结果' },
];

/** 后端 agent_name → 责任阶段（固定映射）。未列出的（如 fast_answer）不计入工作流。 */
const AGENT_TO_STAGE: Record<string, StageId> = {
  scope_clarifier: 'planning',
  request_contract_normalizer: 'planning',
  research_brief_builder: 'planning',
  intent_amendment_router: 'planning',
  destination_geo_resolver: 'planning',
  weather_context_builder: 'planning',
  trip_summary_card_brief: 'planning',
  planner: 'planning',
  dispatcher: 'planning',
  destination_researcher: 'research',
  transport_researcher: 'research',
  accommodation_researcher: 'research',
  itinerary_planner: 'research',
  candidate_gate: 'verify',
  artifact_gate: 'review',
  delivery_quality_gate: 'review',
  delivery_projector: 'synthesis',
  delivery_finalizer: 'synthesis',
  智能调度: 'planning',
  需求确认: 'planning',
  需求合同: 'planning',
  调研简报: 'planning',
  要求更新: 'planning',
  任务规划: 'planning',
  任务分发: 'planning',
  目的地调研: 'research',
  交通查询: 'research',
  住宿查询: 'research',
  行程规划: 'research',
  候选准入: 'verify',
  产物校验: 'review',
  交付质量: 'review',
  交付投影: 'synthesis',
  原子交付: 'synthesis',
};

export function getStageIdForAgent(agentName: string): StageId | null {
  const { base } = parseAgentRound(agentName);
  return AGENT_TO_STAGE[base] ?? null;
}

const STAGE_INDEX: Record<StageId, number> = {
  planning: 0,
  research: 1,
  verify: 2,
  review: 3,
  synthesis: 4,
};

/** 去掉补研轮次后缀 `_rN`（对齐 display_names.py 的 `_rN` 约定），返回基础 agent 名 + 轮次 */
function parseAgentRound(agentName: string): { base: string; round: number } {
  const m = /^(.+?)_r(\d+)$/.exec(agentName);
  if (m) return { base: m[1], round: parseInt(m[2], 10) };
  const display = /^(.+?)（第(\d+)轮补充）$/.exec(agentName);
  if (display) return { base: display[1], round: parseInt(display[2], 10) };
  return { base: agentName, round: 0 };
}

export interface StageView extends StageDef {
  status: StageStatus;
  startedAt?: Date;
  endedAt?: Date;
  elapsedMs?: number;
  stalled?: boolean;
  /** 该阶段触发过的补研轮次（>0 时显示「补充调研 · 第N轮」） */
  refinementRound?: number;
  refinementReason?: string;
}

export interface DerivedStages {
  stages: StageView[];
  /** 是否构成深度工作流（≥2 阶段被触达）；fast 单步问答 → false，不渲染 */
  hasDeepWorkflow: boolean;
}

export interface StageSignals {
  isStreaming: boolean;
  isSynthesizing: boolean;
  /** 最终回答是否已开始产出（synthesizer chat_chunk 到达）——用于推导合成阶段状态 */
  answerStarted: boolean;
}

/**
 * 从 thinkingSteps 派生 5 阶段责任视图。
 *
 * 合成阶段特殊处理：synthesizer 产出的是最终回答（chat_chunk），不产 thinkingStep，
 * 因此用 isSynthesizing / answerStarted 推导其状态，而非靠 step 命中。
 */
export function deriveStages(steps: ThinkingStep[], signals: StageSignals): DerivedStages {
  const touched = new Set<StageId>();
  let lastStageIndex = -1;
  let maxRound = 0;
  const roundByStage: Partial<Record<StageId, number>> = {};
  const timingByStage: Partial<Record<StageId, { startedAt: Date; endedAt?: Date }>> = {};

  for (const step of steps) {
    const { base, round } = parseAgentRound(step.agentName || '');
    const stage = AGENT_TO_STAGE[base];
    if (!stage) continue;
    touched.add(stage);
    lastStageIndex = STAGE_INDEX[stage];
    const startedAt = step.timestamp instanceof Date ? step.timestamp : new Date(step.timestamp);
    const endedAt = step.endTime instanceof Date ? step.endTime : step.endTime ? new Date(step.endTime) : undefined;
    const current = timingByStage[stage];
    if (!current || startedAt < current.startedAt) {
      timingByStage[stage] = { startedAt, endedAt: endedAt ?? current?.endedAt };
    } else if (endedAt && (!current.endedAt || endedAt > current.endedAt)) {
      current.endedAt = endedAt;
    }
    if (round > 0) {
      maxRound = Math.max(maxRound, round);
      roundByStage[stage] = Math.max(roundByStage[stage] ?? 0, round);
    }
  }

  const { isStreaming, isSynthesizing, answerStarted } = signals;

  // 合成阶段状态：synthesizing / 回答已开始流式 → active；流式结束且有回答 → done
  const synthesisActive = isSynthesizing || (isStreaming && answerStarted);
  const synthesisDone = !isStreaming && answerStarted;

  // 计算「当前头部」阶段与其是否 active
  let headIndex: number;
  let headActive: boolean;
  if (synthesisDone) {
    headIndex = STAGE_INDEX.synthesis;
    headActive = false; // 全部完成
  } else if (synthesisActive) {
    headIndex = STAGE_INDEX.synthesis;
    headActive = true; // 合成进行中
  } else if (lastStageIndex >= 0) {
    headIndex = lastStageIndex;
    headActive = isStreaming;
  } else {
    headIndex = -1;
    headActive = false;
  }

  const now = Date.now();
  const stages: StageView[] = STAGE_DEFS.map((def) => {
    const idx = STAGE_INDEX[def.id];
    let status: StageStatus;
    if (headIndex < 0) {
      status = 'pending';
    } else if (idx < headIndex) {
      status = 'done';
    } else if (idx === headIndex) {
      status = headActive ? 'active' : 'done';
    } else {
      // 头部之后：补研可能回访过更晚阶段 → 触达过则 done，否则 pending
      status = touched.has(def.id) ? 'done' : 'pending';
    }

    const timing = timingByStage[def.id];
    const elapsedMs =
      timing?.startedAt
        ? (timing.endedAt?.getTime() ?? (status === 'active' ? now : timing.startedAt.getTime())) -
          timing.startedAt.getTime()
        : undefined;
    const baseView: StageView = {
      ...def,
      status,
      startedAt: timing?.startedAt,
      endedAt: timing?.endedAt,
      elapsedMs,
      stalled: status === 'active' && elapsedMs !== undefined && elapsedMs > 60_000,
    };
    const refinementRound = roundByStage[def.id];
    if (refinementRound) {
      return {
        ...baseView,
        refinementRound,
        refinementReason: '部分信息时效或证据待补充，已自动追加一轮调研核实',
      };
    }
    return baseView;
  });

  // 确定性 Gate 触发 Worker 定向补研时，在成行阶段显示轮次摘要。
  if (maxRound > 0) {
    const review = stages[STAGE_INDEX.review];
    if (!review.refinementRound) {
      review.refinementRound = maxRound;
      review.refinementReason = '校验发现需补充的信息，已触发补充调研';
    }
  }

  return { stages, hasDeepWorkflow: touched.size >= 2 };
}

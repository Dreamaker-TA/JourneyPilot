import type { ThinkingStep } from '../types/chat';
import { deriveStages } from './agentStages';

const AGENT_LABELS: Record<string, { label: string; summary: string }> = {
  scope_clarifier: { label: '确认旅行边界', summary: '检查目的地、日期、同行人和硬约束是否足够明确' },
  request_contract_normalizer: { label: '整理需求合同', summary: '把本次要求和个人约束归一为可追踪的任务合同' },
  research_brief_builder: { label: '生成调研简报', summary: '把需求合同确定性投影成各领域调研目标' },
  intent_amendment_router: { label: '处理新增要求', summary: '判断新增要求的影响范围并更新当前计划' },
  destination_geo_resolver: { label: '确认目的地', summary: '解析目的地位置、时区和旅行范围' },
  weather_context_builder: { label: '准备天气信息', summary: '在安排前准备预报或季节参考' },
  planner: { label: '制定计划', summary: '把旅行需求拆成目的地、交通、住宿和每日安排' },
  dispatcher: { label: '安排调研', summary: '把不同主题交给对应调研分支处理' },
  destination_researcher: { label: '调研目的地体验', summary: '查找景点、美食、文化体验和本地注意事项' },
  transport_researcher: { label: '核对交通衔接', summary: '检查城际和市内交通是否顺路、可执行' },
  accommodation_researcher: { label: '筛选住宿区域', summary: '对比住宿位置、价格、评价和出行便利性' },
  itinerary_planner: { label: '编排每日行程', summary: '把活动、交通和休息节奏排进每天的时间线' },
  candidate_gate: { label: '核对旅行选项', summary: '校验领域字段、硬约束、来源和天气适用性' },
  artifact_gate: { label: '整理行程内容', summary: '确认行程实体完整可用' },
  delivery_quality_gate: { label: '检查行程质量', summary: '检查相邻交通、天气、来源和完整性' },
  delivery_projector: { label: '生成行程内容', summary: '从同一事实快照生成报告、地图和来源索引' },
  delivery_finalizer: { label: '保存行程', summary: '完整保存行程后结束本次规划' },
  fast_answer_agent: { label: '快速回答', summary: '用较轻路径回答简单旅行问题' },
};

export function formatAgentLabel(agentName: string): { label: string; summary: string } {
  const base = agentName.replace(/_r\d+$/, '');
  return AGENT_LABELS[base] || { label: '推进规划任务', summary: '处理本轮旅行规划中的一个步骤' };
}

export function derivePlanningStageSnapshot(args: {
  steps: ThinkingStep[];
  isStreaming: boolean;
  isSynthesizing: boolean;
  answerStarted: boolean;
}) {
  return deriveStages(args.steps, args);
}

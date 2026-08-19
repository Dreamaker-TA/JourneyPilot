import { describe, expect, it } from 'vitest';

import { normalizePlanApprovalGate } from './planApprovalGate';

describe('normalizePlanApprovalGate', () => {
  it('keeps hard requirements, preferences, and attention separate', () => {
    const gate = normalizePlanApprovalGate({
      run_id: 'run_test',
      gate: 'plan',
      payload: {
        plan: {
          steps: [{ step: 1, agents: ['destination_researcher'], tasks: {} }],
        },
        plan_text: 'Research Tokyo',
        decision_options: ['approve', 'cancel'],
        recognized_requirements: {
          hard: [{ requirement_id: 'intent_no_museum', summary: '不安排博物馆' }],
          preferences: [{ requirement_id: 'intent_architecture', summary: '偏好当代建筑' }],
          attention: [{ requirement_id: 'conflict_1', summary: '两项要求相互冲突' }],
        },
      },
    });

    expect(gate?.recognizedRequirements).toEqual({
      hard: [{ requirementId: 'intent_no_museum', summary: '不安排博物馆' }],
      preferences: [{ requirementId: 'intent_architecture', summary: '偏好当代建筑' }],
      attention: [{ requirementId: 'conflict_1', summary: '两项要求相互冲突' }],
    });
  });
});

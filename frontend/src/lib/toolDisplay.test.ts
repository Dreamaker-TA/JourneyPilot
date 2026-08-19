import { describe, expect, it } from 'vitest';

import { looksLikeMachinePayload, toolResultText } from './toolDisplay';
import type { ThinkingStep } from '../types/chat';

function step(toolResult: string | undefined): Pick<ThinkingStep, 'toolResult'> {
  return { toolResult };
}

/**
 * INV-UI-003：产品面永远不印工具原始载荷。
 *
 * 这道闸挡的是后端已经挡不到的那一半：历史会话里存着的 `tool_result` 是当时那版摘要器
 * 留下的，翻回去照样会把 `{"success": true, "provider": "nominatim", …}` 整坨铺在思维链上。
 * 「该说什么」仍然只有后端一个 owner，这里只决定「不该印什么」。
 */
describe('toolResultText', () => {
  it('keeps a traveller-readable summary', () => {
    expect(toolResultText(step('找到 4 个结果：深圳湾公园、红树林、人才公园…'))).toBe(
      '找到 4 个结果：深圳湾公园、红树林、人才公园…'
    );
    expect(toolResultText(step('地铁 · 深圳北站 → 深圳湾公园 · 约 42 分钟'))).toBe(
      '地铁 · 深圳北站 → 深圳湾公园 · 约 42 分钟'
    );
  });

  it('refuses a raw provider payload', () => {
    // 两张截图里真实漏出来的那两条。
    expect(
      toolResultText(step('{"success": true, "provider": "nominatim", "query": "深圳湾滨海休闲带"}'))
    ).toBeNull();
    expect(
      toolResultText(step('{"freshness_hint": {"published_at": ""}, "observed_at": "2026-08-19T10:26:29"}'))
    ).toBeNull();
    expect(toolResultText(step('[{"text": "{\\"success\\": true}"}]'))).toBeNull();
  });

  it('refuses a payload that leads with whitespace', () => {
    expect(toolResultText(step('\n  {"success": true}'))).toBeNull();
  });

  it('has nothing to print without a summary', () => {
    expect(toolResultText(step(undefined))).toBeNull();
    expect(toolResultText(step('   '))).toBeNull();
  });

  it('judges by the opening bracket only', () => {
    expect(looksLikeMachinePayload('{"a": 1}')).toBe(true);
    expect(looksLikeMachinePayload('[1, 2]')).toBe(true);
    // 一句话里带花括号不算载荷：判据必须在开头，否则会误伤正常摘要。
    expect(looksLikeMachinePayload('找到 3 条网页：什么是 {json}')).toBe(false);
  });
});

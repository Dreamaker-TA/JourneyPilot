import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { ApiError } from './api';
import {
  KNOWLEDGE_FAILURE_CODES,
  describeKnowledgeIngestFailure,
  describeKnowledgeSourceFailure,
} from './knowledgeIngestFailure';

/**
 * 这一批钉住的是**跨语言的那道界**：后端能发的每一个资料库 4xx `code`，界面这边都要
 * 有自己的一句话。少一个的后果不是「少一句话」，是**画成另一句话** —— 界面读不到 code
 * 就按状态码回落，而 422 那一档的回落是「请求的信息不完整，请刷新页面后重试」，
 * 一份损坏的 PDF 刷一百次还是损坏的。
 *
 * 所以这里不写死一份期望清单，而是**去读后端那个文件**：两张表的差集必须为空，
 * 而且这个断言在任何一侧改动时都会响。
 */

function backendSource(): string {
  const here = fileURLToPath(new URL('.', import.meta.url));
  return readFileSync(
    `${here}../../../src/travel_agent/api/routes/knowledge.py`,
    'utf-8',
  );
}

/** 后端那条路上出现的全部 4xx code。 */
function backendCodes(): Set<string> {
  const source = backendSource();
  const codes = new Set<string>();
  // 两种写法都要抓到：`"code": "x"` 的字面量，和 `_REJECTION_MESSAGE` 那张表的键。
  for (const match of source.matchAll(/"code":\s*"([a-z_]+)"/g)) {
    codes.add(match[1]);
  }
  const table = source.match(/_REJECTION_MESSAGE\s*=\s*\{([\s\S]*?)\n\}/);
  if (table) {
    for (const match of table[1].matchAll(/"([a-z_]+)":/g)) {
      codes.add(match[1]);
    }
  }
  return codes;
}

function apiError(status: number, code?: string): ApiError {
  return new ApiError('boom', status, code ? { detail: { code } } : { detail: 'plain' });
}

describe('资料库失败 code 的两张表', () => {
  it('后端能发的每一个 code 界面都认识', () => {
    const known = new Set<string>(KNOWLEDGE_FAILURE_CODES);
    const missing = [...backendCodes()].filter((code) => !known.has(code));
    expect(missing, `后端有而界面没有的 code（会被画成另一种失败）`).toEqual([]);
  });

  it('界面认识的每一个 code 后端真的会发', () => {
    const backend = backendCodes();
    const unreachable = KNOWLEDGE_FAILURE_CODES.filter((code) => !backend.has(code));
    expect(unreachable, '界面有而后端从不发的 code（永不执行的分支）').toEqual([]);
  });

  it('每一个 code 都换来一句自己的话，而不是状态码回落', () => {
    const fallback = describeKnowledgeIngestFailure(apiError(422), '上传').message;
    for (const code of KNOWLEDGE_FAILURE_CODES) {
      const failure = describeKnowledgeIngestFailure(apiError(422, code), '上传');
      expect(failure.message, code).not.toEqual(fallback);
      expect(failure.message.length, code).toBeGreaterThan(0);
    }
  });
});

describe('恢复动作与失败的性质一致', () => {
  it('文件本身的问题不给重试键', () => {
    for (const code of [
      'unsupported_file_type',
      'file_too_large',
      'document_unreadable',
      'document_too_complex',
      'document_parse_timeout',
    ]) {
      const failure = describeKnowledgeIngestFailure(apiError(422, code), '上传');
      expect(failure.recovery, code).toBe('none');
    }
  });

  it('服务器忙不过来给重试键 —— 这一种换文件没用', () => {
    const failure = describeKnowledgeIngestFailure(apiError(503, 'ingest_busy'), '上传');
    expect(failure.recovery).toBe('retry');
  });

  it('寻址脏了给刷新键', () => {
    const failure = describeKnowledgeIngestFailure(
      apiError(400, 'collection_address_invalid'),
      '上传',
    );
    expect(failure.recovery).toBe('reload');
  });
});

describe('同一个 code 按输入种类说话', () => {
  it('no_indexable_text 对文件与手输给不同的下一步', () => {
    const upload = describeKnowledgeIngestFailure(apiError(422, 'no_indexable_text'), '上传');
    const typed = describeKnowledgeIngestFailure(apiError(422, 'no_indexable_text'), '添加');
    expect(upload.message).not.toEqual(typed.message);
    expect(upload.message).toContain('扫描版');
  });

  it('单篇资料的失败回落说的是「这篇」而不是整库', () => {
    const failure = describeKnowledgeSourceFailure(apiError(500), '读取');
    expect(failure.message).toContain('这篇资料');
  });
});

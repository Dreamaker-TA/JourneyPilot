import { expect, test } from '@playwright/test';

/**
 * 冒烟：这套服务**起来了并且能被打开**。
 *
 * 刻意不在这里断言规划结果：那需要一个真实模型，而 CI 不花付费额度
 * （ADR-P1-06）。这一层要抓的是「部署本身坏了」那一类 —— 静态资源没构建、
 * 前端拿不到 API、readiness 在撒谎。规划链路的正确性由后端的合同与集成测试承担。
 */

test('readiness 报出每个组件的结论', async ({ request, baseURL }) => {
  const response = await request.get(`${baseURL}/api/health/ready`);
  // 200 或 503 都是**回答**；连不上才是失败。降级启动的部署应当照实说自己降级了。
  expect([200, 503]).toContain(response.status());

  const body = await response.json();
  expect(body.status).toMatch(/^(ready|not_ready)$/);
  // 这几项是 P0/P1 落下来的读数通道，缺一项就说明有一层的状态没有出口。
  for (const component of [
    'database',
    'redis',
    'database_schema',
    'run_execution',
    'background_jobs',
    'resource_limits',
    'optional_capabilities',
    'pdf_export',
  ]) {
    expect(body.components, component).toHaveProperty(component);
    expect(body.components[component], component).toHaveProperty('ready');
  }
});

test('网页端能打开并且拿到前端构建产物', async ({ page }) => {
  const failures: string[] = [];
  page.on('requestfailed', (request) => {
    failures.push(`${request.method()} ${request.url()}`);
  });

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#root')).toBeAttached();
  // 挂载成功意味着 JS bundle 真的执行了 —— 只断言 HTML 到了会漏掉「构建产物没更新」。
  await expect(page.locator('#root')).not.toBeEmpty({ timeout: 15_000 });
  expect(failures, '有资源加载失败').toEqual([]);
});

test('资料库上传拒绝一份伪装成 PDF 的文件', async ({ request, baseURL }) => {
  // 这一条把 P1-06 的输入边界钉在真实 HTTP 上：magic bytes 不对就不该进解析器，
  // 而回执必须带 code —— 界面按 code 说话，读不到就只能印一句错的话。
  const response = await request.post(`${baseURL}/api/knowledge/upload-file`, {
    multipart: {
      file: {
        name: 'not-really.pdf',
        mimeType: 'application/pdf',
        buffer: Buffer.from('这其实是一段纯文本，不是 PDF', 'utf-8'),
      },
      collection: 'default',
    },
  });

  expect(response.status()).toBe(422);
  const body = await response.json();
  expect(body.detail.code).toBe('document_unreadable');
});

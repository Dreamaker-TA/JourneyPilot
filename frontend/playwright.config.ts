import { defineConfig, devices } from '@playwright/test';

/**
 * E2E 跑在一套**真实跑着的** JourneyPilot 上（`./run.sh start` 或
 * `docker compose up`），不由这份配置去拉起后端：那条启动路径有自己的编排器
 * （`journeypilot` CLI + main.py），在这里复制一份等于让「怎么启动」有两个 owner。
 *
 * 地址来自环境变量，默认是 run.sh 的端口。CI 的 e2e-smoke 作业先起 compose 再传入。
 */
const baseURL = process.env.JOURNEYPILOT_APP_URL || 'http://127.0.0.1:8080';

export default defineConfig({
  testDir: './e2e',
  // CI 上禁止 .only：一个漏掉的 .only 会让整套门禁静默只跑一个用例。
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI
    ? [['list'], ['junit', { outputFile: 'test-results/playwright-junit.xml' }]]
    : 'list',
  use: {
    baseURL,
    // 失败时留下能看的东西：一次 CI 上的 e2e 失败如果只有一行报错，等于没有诊断。
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});

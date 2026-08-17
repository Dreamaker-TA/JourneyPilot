import { defineConfig } from 'vitest/config';

// 单元测试跑在 node 环境里：这一批测的是纯函数（错误 detail 读取、失败文案表、
// SSE 解码），不是组件渲染。给它们一个 jsdom 等于为了没人需要的东西付启动时间。
//
// **刻意不复用 vite.config.ts**：那份带 react 插件、代理和 manualChunks，
// 它们对测试没有一条是必要的，而 build 配置改动不该让测试行为跟着变。
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    // Playwright 的 e2e 规格不在这里跑（它需要一个真实浏览器和一套跑着的服务）。
    exclude: ['e2e/**', 'node_modules/**'],
    reporters: process.env.CI ? ['default', 'junit'] : ['default'],
    outputFile: { junit: 'test-results/vitest-junit.xml' },
  },
});

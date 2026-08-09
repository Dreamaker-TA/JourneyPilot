import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // 子路径部署时通过环境变量覆盖 base / 输出目录：
  //   VITE_BASE=/journeypilot/ VITE_OUT_DIR=dist npm run build
  base: process.env.VITE_BASE || '/',
  server: {
    host: process.env.HOST || '0.0.0.0',
    port: Number(process.env.PORT) || 8080,
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8001',
        changeOrigin: true,
      }
    }
  },
  build: {
    outDir: process.env.VITE_OUT_DIR || '../static',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          // react-markdown 生态（remark / rehype / mdast / micromark / unified /
          // vfile / property-information 等，合计约 91kb gz）是 chat 首屏 Markdown
          // 渲染的必需依赖，按设计保留为静态 import（拆成懒加载会拖慢聊天首帧）。
          // 但它是一整棵稳定的第三方依赖树，从 app 入口 chunk 拆出到独立 vendor
          // chunk：① 与入口并行下载，首屏不增延迟；② 第三方代码变动频率低，独立
          // 长缓存命中率高；③ 把 app 入口的 minified raw 体积降到 500kb 阈值下，
          // 消除 chunk 体积警告（IX-R3 / DESIGN-INTERACTION §11-R3）。
          // 注意：motion 的 domMax 拆分由 LazyMotion + CanvasMotion 动态 import
          // 自动完成（motion-features chunk），此处不触碰，避免把 domMax 拉回主包。
          if (id.includes('node_modules')) {
            // react / react-dom / scheduler：全站最稳定的框架内核，独立成
            // react-vendor chunk——长缓存命中率最高，且把它从 app 入口移出后
            // 入口 minified raw 降到 500kb 阈值下，消除体积警告。
            if (/[/\\]node_modules[/\\](react|react-dom|scheduler|use-sync-external-store)[/\\]/.test(id)) {
              return 'react-vendor';
            }
            if (
              /[/\\]node_modules[/\\](react-markdown|remark-gfm|remark-|rehype-|mdast-|micromark|unified|vfile|property-information|hast-|unist-|decode-named-character-reference|character-entities|comma-separated-tokens|space-separated-tokens|html-url-attributes|trim-lines|ccount|escape-string-regexp|markdown-table|zwitch|longest-streak|devlop|bail|is-plain-obj|trough|extend|estree-util-is-identifier-name)/.test(
                id
              )
            ) {
              return 'markdown-vendor';
            }
          }
        },
      },
    },
  }
})

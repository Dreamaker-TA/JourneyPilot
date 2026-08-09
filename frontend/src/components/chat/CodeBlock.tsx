import React, { Suspense, lazy } from 'react';

/**
 * 语法高亮代码块（懒加载分包）。
 *
 * 两层瘦身，只影响 chat/Markdown.tsx 这一个消费方：
 *
 * 一层——**PrismLight + 显式注册语言**：改用 react-syntax-highlighter 的 Light
 * build（`PrismLight`），只注册产品输出里可能出现的少量语言（json / javascript /
 * typescript / python / bash / yaml / markdown）。这样 refractor 的全语言包
 * （~175kb gz、构建时炸成数百个语言 chunk）整体消失——旅行规划正文本就几乎不含
 * 代码块，注册这一小撮已覆盖偶发的 API/配置片段。
 *
 * 二层——**注册后的高亮器仍 React.lazy**：代码块先以无高亮的 `<pre><code>`
 * 立即渲染（内容即刻可读，等宽字体 + 背景样式与高亮版一致，避免布局跳动），高亮
 * chunk 到达后就地升级着色。整段完全在 chat 首屏关键路径之外。
 *
 * react-markdown 生态（~91kb gz）不在此拆——每条助手消息首屏渲染必需，拆它会拖慢
 * 聊天首帧，违背「操作必答」。inline code 与普通正文不经过本组件，零影响。
 */
const Highlighter = lazy(async () => {
  const [prismMod, styleMod, json, javascript, typescript, python, bash, yaml, markdown] =
    await Promise.all([
      import('react-syntax-highlighter/dist/esm/prism-light'),
      import('react-syntax-highlighter/dist/esm/styles/prism/one-light'),
      import('react-syntax-highlighter/dist/esm/languages/prism/json'),
      import('react-syntax-highlighter/dist/esm/languages/prism/javascript'),
      import('react-syntax-highlighter/dist/esm/languages/prism/typescript'),
      import('react-syntax-highlighter/dist/esm/languages/prism/python'),
      import('react-syntax-highlighter/dist/esm/languages/prism/bash'),
      import('react-syntax-highlighter/dist/esm/languages/prism/yaml'),
      import('react-syntax-highlighter/dist/esm/languages/prism/markdown'),
    ]);

  const PrismLight = prismMod.default;
  PrismLight.registerLanguage('json', json.default);
  PrismLight.registerLanguage('javascript', javascript.default);
  PrismLight.registerLanguage('typescript', typescript.default);
  PrismLight.registerLanguage('python', python.default);
  PrismLight.registerLanguage('bash', bash.default);
  PrismLight.registerLanguage('yaml', yaml.default);
  PrismLight.registerLanguage('markdown', markdown.default);

  const oneLight = styleMod.default;
  const Colored: React.FC<{ language: string; code: string }> = ({ language, code }) => (
    <PrismLight
      style={oneLight}
      language={language}
      PreTag="div"
      customStyle={{ borderRadius: '8px', fontSize: '13px', margin: '8px 0' }}
    >
      {code}
    </PrismLight>
  );
  return { default: Colored };
});

/**
 * 高亮器 chunk 未到达时的即时占位——朴素等宽 `<pre><code>`，圆角/内边距/背景与
 * 高亮版外框一致，chunk 到达升级着色时不产生布局跳动（§11-R3 追加验收）。
 */
const CodeFallback: React.FC<{ code: string }> = ({ code }) => (
  <pre
    className="overflow-x-auto rounded-card bg-ink/[0.03] p-3 text-[13px] leading-relaxed"
    style={{ margin: '8px 0' }}
  >
    <code>{code}</code>
  </pre>
);

export const CodeBlock: React.FC<{ language: string; code: string }> = ({ language, code }) => (
  <Suspense fallback={<CodeFallback code={code} />}>
    <Highlighter language={language} code={code} />
  </Suspense>
);

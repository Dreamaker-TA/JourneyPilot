import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { cn } from '../../lib/utils';
import { safeHref } from '../../lib/safeHref';
import type { FinalAnswerCitation, InformationAnnotation } from '../../types/chat';
import { CodeBlock } from './CodeBlock';
import { CitationMarker } from '../citations/CitationMarker';
import { InformationAnnotationMarker } from '../citations/InformationAnnotationMarker';

/** 助手正文与 waiting_input 内嵌正文共用的 Markdown 渲染组件。 */
const markdownComponents = {
  code({ className, children, ...props }: { className?: string; children?: React.ReactNode }) {
    const match = /language-(\w+)/.exec(className || '');
    const codeStr = String(children).replace(/\n$/, '');
    if (match) {
      // 语法高亮器（~189kb gz）懒加载进独立 chunk，只有出现围栏代码块时才下载。
      return <CodeBlock language={match[1]} code={codeStr} />;
    }
    return (
      <code className="bg-ink/5 px-1.5 py-0.5 rounded-label text-[13px]" {...props}>
        {children}
      </code>
    );
  },
  table({ children }: { children?: React.ReactNode }) {
    return (
      <div className="overflow-x-auto my-2">
        <table className="w-full text-xs border-collapse">{children}</table>
      </div>
    );
  },
  th({ children }: { children?: React.ReactNode }) {
    return (
      <th className="px-3 py-2 text-left font-semibold bg-ink/[0.03] border-b border-stroke/60">
        {children}
      </th>
    );
  },
  td({ children }: { children?: React.ReactNode }) {
    return <td className="px-3 py-2 border-b border-stroke/60">{children}</td>;
  },
};

const SAFE_CITATION_RE = /\[\[\s*cite\s*[:：]\s*([a-z0-9_-]+)\s*\]\]/gi;
const SAFE_ANNOTATION_RE = /\[\[\s*annotation\s*[:：]\s*([a-z0-9_-]+)\s*\]\]/gi;

/** 只渲染后端当前 cite contract 中有结构化绑定的锚点。 */
export function prepareCitationMarkdown(
  content: string,
  citations: FinalAnswerCitation[] = [],
  annotations: InformationAnnotation[] = []
): string {
  const citationIds = new Set(citations.map((citation) => citation.citationId));
  const annotationIds = new Set(annotations.map((annotation) => annotation.annotationId));
  return content.replace(SAFE_CITATION_RE, (_marker, citationId: string) =>
    citationIds.has(citationId) ? `[来源](#jp-citation-${citationId})` : ''
  ).replace(SAFE_ANNOTATION_RE, (_marker, annotationId: string) =>
    annotationIds.has(annotationId) ? `[信息状态](#jp-annotation-${annotationId})` : ''
  );
}

export function prepareCitationCopyText(
  content: string,
  citations: FinalAnswerCitation[] = [],
  annotations: InformationAnnotation[] = []
): string {
  const indexById = new Map(citations.map((citation, index) => [citation.citationId, index + 1]));
  const annotationById = new Map(annotations.map((annotation) => [annotation.annotationId, annotation]));
  const cleanBody = content.replace(SAFE_CITATION_RE, (_marker, citationId: string) => {
    const index = indexById.get(citationId);
    return index ? `[${index}]` : '';
  }).replace(SAFE_ANNOTATION_RE, (_marker, annotationId: string) => {
    const annotation = annotationById.get(annotationId);
    return annotation ? `（${annotation.label}）` : '';
  }).trim();
  const seenSources = new Set<string>();
  const sourceRows: string[] = [];
  citations.forEach((citation, citationIndex) => {
    citation.sources.forEach((source, sourceIndex) => {
      if (!source.title && !source.sourceName && !source.url) return;
      const label = source.title || source.sourceName || `参考来源 ${sourceIndex + 1}`;
      const key = source.url || label;
      if (seenSources.has(key)) return;
      seenSources.add(key);
      sourceRows.push(`[${citationIndex + 1}] ${label}${source.url ? ` ${source.url}` : ''}`);
    });
  });
  return sourceRows.length > 0 ? `${cleanBody}\n\n来源：\n${sourceRows.join('\n')}` : cleanBody;
}

export const Markdown: React.FC<{
  content: string;
  citations?: FinalAnswerCitation[];
  annotations?: InformationAnnotation[];
  streaming?: boolean;
  className?: string;
}> = ({
  content,
  citations = [],
  annotations = [],
  streaming,
  className,
}) => {
  const preparedContent = React.useMemo(
    () => prepareCitationMarkdown(content, citations, annotations),
    [content, citations, annotations]
  );
  const components = React.useMemo(() => ({
    ...markdownComponents,
    a({ href, children }: { href?: string; children?: React.ReactNode }) {
      const citationId = href?.startsWith('#jp-citation-')
        ? href.slice('#jp-citation-'.length)
        : '';
      const citationIndex = citations.findIndex((item) => item.citationId === citationId);
      if (citationIndex >= 0) {
        return <CitationMarker citation={citations[citationIndex]} />;
      }
      const annotationId = href?.startsWith('#jp-annotation-')
        ? href.slice('#jp-annotation-'.length)
        : '';
      const annotation = annotations.find((item) => item.annotationId === annotationId);
      if (annotation) {
        return <InformationAnnotationMarker annotation={annotation} />;
      }
      // 仅 http(s)/mailto/#/相对路径；javascript: 等渲染为不可点文本。
      const hrefSafe = safeHref(href);
      if (!hrefSafe) {
        return <span className="text-ink-secondary">{children}</span>;
      }
      const isHash = hrefSafe.startsWith('#');
      return (
        <a
          href={hrefSafe}
          {...(isHash ? {} : { target: '_blank', rel: 'noopener noreferrer' })}
        >
          {children}
        </a>
      );
    },
  }), [annotations, citations]);

  return (
    <div
      className={cn(
        'prose-sm max-w-none text-sm leading-relaxed markdown-content min-w-0',
        streaming && 'is-streaming',
        className
      )}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {preparedContent}
      </ReactMarkdown>
    </div>
  );
};

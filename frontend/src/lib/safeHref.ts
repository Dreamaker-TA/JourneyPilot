/**
 * Markdown / 链接安全：仅允许 http(s)、mailto、页内 # 锚点，以及无 scheme 的相对路径。
 * 拒绝 javascript:/data:/vbscript: 等危险协议，以及协议相对 //host（易被当站内相对路径误放）。
 */

const ALLOWED_SCHEMES = new Set(['http:', 'https:', 'mailto:']);

/** 是否可作为安全 href 使用。 */
export function isSafeHref(href: string | null | undefined): boolean {
  if (href == null) return false;
  const trimmed = href.trim();
  if (!trimmed) return false;

  // 页内锚点（含 #jp-citation-*）
  if (trimmed.startsWith('#')) return true;

  // 协议相对 URL：//evil.example — 不当相对路径放行
  if (trimmed.startsWith('//')) return false;

  const schemeMatch = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(trimmed);
  if (schemeMatch) {
    return ALLOWED_SCHEMES.has(`${schemeMatch[1].toLowerCase()}:`);
  }

  // 无 scheme：绝对路径 /foo、相对 ./ ../ 或 path-only
  return true;
}

/** 安全则返回 trim 后的 href，否则 undefined（调用方渲染为不可点文本）。 */
export function safeHref(href: string | null | undefined): string | undefined {
  if (!isSafeHref(href)) return undefined;
  return href!.trim();
}

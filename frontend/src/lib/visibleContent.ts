const DESTINATION_JSON_BLOCK = /<!--\s*DESTINATION_JSON\b[\s\S]*?-->/gi;
const DESTINATION_JSON_OPEN = /<!--\s*DESTINATION_JSON\b/i;

/**
 * Session history may contain the model's structured side-channel because older
 * turns persisted the raw completion even though SSE hid it. Keep the same
 * user-visible boundary when hydrating those immutable history records.
 */
export function stripAssistantControlBlocks(content: string): string {
  const withoutCompleteBlocks = content.replace(DESTINATION_JSON_BLOCK, '');
  const unterminatedStart = withoutCompleteBlocks.search(DESTINATION_JSON_OPEN);
  const visible = unterminatedStart >= 0
    ? withoutCompleteBlocks.slice(0, unterminatedStart)
    : withoutCompleteBlocks;
  return visible.trimEnd();
}

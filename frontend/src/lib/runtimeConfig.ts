type ImportMetaWithEnv = ImportMeta & {
  env?: {
    VITE_API_BASE?: string;
  };
};

function readEnv(name: 'VITE_API_BASE'): string | undefined {
  return (import.meta as ImportMetaWithEnv).env?.[name];
}

/**
 * The api-base decision itself, separated from where the value comes from.
 *
 * Vite freezes a source module's `import.meta.env` at transform time, so the
 * base cannot be re-derived after the module loads — keep this pure and free
 * of `import.meta` reads.
 */
export function resolveApiBaseUrl(configured: string | undefined): string {
  const raw = configured?.trim();
  if (!raw) return '/api';
  return raw.replace(/\/+$/, '');
}

export function getApiBaseUrl(): string {
  return resolveApiBaseUrl(readEnv('VITE_API_BASE'));
}

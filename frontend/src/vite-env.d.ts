/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Where the API lives. Defaults to /api, which the dev server proxies. */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

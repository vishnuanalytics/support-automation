/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_SUPABASE_URL: string;
  readonly VITE_SUPABASE_ANON_KEY: string;
  readonly VITE_API_BASE?: string;
  // Both optional -- unset means GTM/GA4 never load at all. See src/analytics.ts.
  readonly VITE_GTM_CONTAINER_ID?: string;
  readonly VITE_GA4_MEASUREMENT_ID?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}

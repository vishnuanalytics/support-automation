import * as Sentry from "@sentry/react";

/**
 * Error monitoring — a no-op unless VITE_SENTRY_DSN is set (same pattern
 * as analytics.ts's GTM/GA4: off by default, nothing loads or sends
 * anything for an operator who hasn't created a Sentry project yet).
 *
 * Unlike GTM/GA4, this runs everywhere -- the signed-in product included,
 * not just the public marketing site -- because catching a real crash in
 * the actual app (not just the landing page) is the entire point. No
 * request bodies or extra PII are attached beyond what Sentry's browser
 * SDK captures by default (the error itself, a stack trace, basic browser/
 * URL context); session replay and performance tracing are both off
 * unless explicitly turned up via env var.
 */
const DSN = import.meta.env.VITE_SENTRY_DSN as string | undefined;

export const sentryConfigured = Boolean(DSN);

export function initSentry(): void {
  if (!DSN) return;
  Sentry.init({
    dsn: DSN,
    environment: import.meta.env.MODE,
    tracesSampleRate: 0,
  });
}

export { Sentry };

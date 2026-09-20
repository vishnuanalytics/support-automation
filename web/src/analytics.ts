/**
 * GTM + GA4 — both optional, independently configurable, and OFF entirely
 * unless their env var is set (VITE_GTM_CONTAINER_ID / VITE_GA4_MEASUREMENT_ID
 * in web/.env.local), so dev and test never fire real tracking, and nothing
 * loads at all for an operator who hasn't created these yet.
 *
 * Scoped to the public marketing site only (see auth/PreAuth.tsx) -- this
 * never runs once someone is signed into the actual product, which stays
 * fully free of third-party tracking. That's a deliberate choice: seeing
 * how many visitors reach the sign-in button is one thing, funneling a
 * real customer's product usage through Google is a much bigger privacy
 * question this repo hasn't been asked to take on.
 *
 * Google Consent Mode v2: both scripts are told "denied" by default before
 * they're ever loaded, and only flipped to "granted" once the visitor
 * accepts via CookieConsentBanner.tsx -- required for EEA traffic since
 * March 2024, and the right default everywhere else too. See
 * landing/CookiePolicy.tsx for the user-facing explanation of this.
 *
 * Setting BOTH env vars loads GA4 twice if the GTM container is *also*
 * configured (inside Google's own Tag Manager UI, not here) with a GA4
 * Configuration tag pointed at the same measurement ID -- that double-
 * counts every pageview. Pick one: either set only
 * VITE_GTM_CONTAINER_ID and manage GA4 (and anything else) as a tag
 * inside that container, or set only VITE_GA4_MEASUREMENT_ID for GA4
 * alone with no Tag Manager layer. Only set both if GTM is deliberately
 * *not* also configured to fire GA4 itself.
 */

const GTM_ID = import.meta.env.VITE_GTM_CONTAINER_ID as string | undefined;
const GA4_ID = import.meta.env.VITE_GA4_MEASUREMENT_ID as string | undefined;

const CONSENT_KEY = "cookie-consent"; // "granted" | "denied", in localStorage

export const analyticsConfigured = Boolean(GTM_ID || GA4_ID);

declare global {
  interface Window {
    dataLayer?: unknown[];
    gtag?: (...args: unknown[]) => void;
  }
}

function gtag(...args: unknown[]) {
  window.dataLayer = window.dataLayer || [];
  window.dataLayer.push(args);
}

function loadScript(src: string) {
  const s = document.createElement("script");
  s.async = true;
  s.src = src;
  document.head.appendChild(s);
}

export function getStoredConsent(): "granted" | "denied" | null {
  try {
    return localStorage.getItem(CONSENT_KEY) as "granted" | "denied" | null;
  } catch {
    return null; // private-mode / storage-blocked browsers -- treat as undecided
  }
}

export function applyConsent(granted: boolean): void {
  try {
    localStorage.setItem(CONSENT_KEY, granted ? "granted" : "denied");
  } catch {
    /* consent still applies for this session even if it can't be remembered */
  }
  gtag("consent", "update", {
    analytics_storage: granted ? "granted" : "denied",
    ad_storage: granted ? "granted" : "denied",
    ad_user_data: granted ? "granted" : "denied",
    ad_personalization: granted ? "granted" : "denied",
  });
}

let initialized = false;

/** Call once, on mount of the public marketing shell. A no-op if neither
 * env var is set, or if already called this session. */
export function initAnalytics(): void {
  if (initialized || !analyticsConfigured) return;
  initialized = true;

  gtag("js", new Date());
  gtag("consent", "default", {
    analytics_storage: "denied",
    ad_storage: "denied",
    ad_user_data: "denied",
    ad_personalization: "denied",
  });
  if (getStoredConsent() === "granted") applyConsent(true);

  // GTM's usual <noscript><iframe> fallback is skipped on purpose -- this
  // whole app requires JS to render anything at all (a JS-disabled visitor
  // sees a blank page regardless), so it would provide zero real coverage.
  if (GTM_ID) loadScript(`https://www.googletagmanager.com/gtm.js?id=${GTM_ID}`);
  if (GA4_ID) {
    loadScript(`https://www.googletagmanager.com/gtag/js?id=${GA4_ID}`);
    gtag("config", GA4_ID);
  }
}

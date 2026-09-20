import { useEffect, useState } from "react";
import { Button } from "../ui";
import { analyticsConfigured, applyConsent, getStoredConsent, initAnalytics } from "../analytics";

/**
 * Only ever rendered on the public marketing site (see PublicShell.tsx) --
 * the product itself, once signed in, loads no third-party tracking at
 * all, so there's nothing to ask consent for there. A no-op if neither
 * VITE_GTM_CONTAINER_ID nor VITE_GA4_MEASUREMENT_ID is set (an operator
 * who hasn't created those yet sees no banner at all, and no analytics
 * script even attempts to load).
 */
export function CookieConsentBanner({ onViewPolicy }: { onViewPolicy: () => void }) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    initAnalytics();
    if (analyticsConfigured && getStoredConsent() === null) setVisible(true);
  }, []);

  if (!visible) return null;

  function choose(granted: boolean) {
    applyConsent(granted);
    setVisible(false);
  }

  return (
    <div className="cookie-banner" role="dialog" aria-label="Cookie consent">
      <p className="cookie-banner__text">
        We use analytics cookies on this page to see how visitors find their way to sign-in —
        nothing about your account or product usage.{" "}
        <button type="button" className="public-link" onClick={onViewPolicy}>
          Cookie Policy
        </button>
      </p>
      <div className="row" style={{ gap: 8, flex: "none" }}>
        <Button variant="ghost" size="sm" onClick={() => choose(false)}>Reject</Button>
        <Button variant="primary" size="sm" onClick={() => choose(true)}>Accept</Button>
      </div>
    </div>
  );
}

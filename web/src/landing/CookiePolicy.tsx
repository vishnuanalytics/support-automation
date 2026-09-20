const CONTACT_EMAIL = "gundamvishnu7@gmail.com";
const EFFECTIVE_DATE = "20 September 2026";

/**
 * Same placeholder convention as PrivacyPolicy.tsx / TermsOfService.tsx --
 * fill in the entity name below together with those two.
 *
 * Written to match what this codebase actually does, not a generic
 * template: the product itself (once signed in) loads no third-party
 * tracking at all -- GTM/GA4 (both optional, off entirely unless
 * VITE_GTM_CONTAINER_ID / VITE_GA4_MEASUREMENT_ID are set -- see
 * src/analytics.ts) only ever run on the public marketing site (this
 * landing page, sign-in, and these legal pages), gated behind Google
 * Consent Mode v2 with both signals defaulted to "denied" until the
 * visitor actually accepts via CookieConsentBanner.
 */
export function CookiePolicy() {
  return (
    <article className="legal-doc">
      <h1>Cookie Policy</h1>
      <p className="legal-doc__meta">Effective {EFFECTIVE_DATE}</p>

      <p>
        This Cookie Policy explains how <strong>[YOUR REGISTERED BUSINESS / ENTITY NAME]</strong>{" "}
        ("we", "us") uses cookies and similar storage (such as browser local storage) on this
        site, and your choices about them. It supplements our <strong>Privacy Policy</strong>.
      </p>

      <h2>1. Two different places, two different rules</h2>
      <p>
        Once you're signed in and using the product itself, we don't load any third-party
        analytics, advertising, or tracking script of any kind — nothing described below applies
        there. This policy is only about the <strong>public pages</strong>: this landing page, the
        sign-in screen, and the legal pages you're reading now.
      </p>

      <h2>2. Strictly necessary storage (no consent needed)</h2>
      <p>
        These are required for the site to function and are exempt from consent requirements
        under applicable law — there's no option to disable them individually, only by not using
        the site:
      </p>
      <ul>
        <li><strong>Authentication:</strong> Supabase's own session storage, so you stay signed in.</li>
        <li><strong>Theme preference:</strong> your light/dark choice, in browser local storage.</li>
        <li><strong>Cookie consent choice:</strong> whether you accepted or rejected analytics cookies (see below), so we don't ask again every visit.</li>
      </ul>

      <h2>3. Analytics cookies (only with your consent)</h2>
      <p>
        On the public pages only, we optionally use <strong>Google Tag Manager</strong> and{" "}
        <strong>Google Analytics 4</strong> to understand aggregate visitor behavior (e.g. how many
        people reach the sign-in button) — never anything about your account or product usage
        once signed in. These are off by default: nothing loads, and no cookie is set, until you
        click <strong>Accept</strong> on the cookie banner. You can withdraw consent at any time by
        clearing this site's data in your browser, which resets the banner on your next visit.
      </p>
      <p>
        If enabled, Google Analytics may set cookies such as <code>_ga</code> and{" "}
        <code>_ga_&lt;container-id&gt;</code>, used only in aggregate and not to build a profile
        tied to your product account. See{" "}
        <a href="https://policies.google.com/privacy" target="_blank" rel="noreferrer">
          Google's own Privacy Policy
        </a>{" "}
        for how Google itself processes this data.
      </p>

      <h2>4. Fonts</h2>
      <p>
        This site loads the Inter typeface from Google Fonts' CDN, which involves your browser
        connecting directly to Google's servers. This isn't a cookie, but it does mean your IP
        address is visible to Google when a page loads, independent of the analytics consent
        above.
      </p>

      <h2>5. Your choices</h2>
      <p>
        Beyond the consent banner, most browsers let you block or delete cookies in their
        settings — doing so may affect how the site behaves (for example, you may be asked to sign
        in more often).
      </p>

      <h2>6. Changes to this policy</h2>
      <p>We may update this policy from time to time; the effective date above reflects the latest version.</p>

      <h2>7. Contact us</h2>
      <p>
        Questions about this policy: <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
      </p>
    </article>
  );
}

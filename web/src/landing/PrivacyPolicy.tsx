const CONTACT_EMAIL = "gundamvishnu7@gmail.com";
const EFFECTIVE_DATE = "17 September 2026";

/**
 * Written to align with India's Digital Personal Data Protection Act, 2023
 * (DPDPA) and the Information Technology (Reasonable Security Practices and
 * Procedures and Sensitive Personal Data or Information) Rules, 2011. The
 * technical claims below (Row-Level Security tenant isolation, credential
 * encryption via Supabase Vault, TLS in transit) are the actual practices
 * in this codebase, not boilerplate — keep this in sync if either changes.
 *
 * The entity name / registered address / grievance officer name are
 * deliberately bracketed placeholders — a Privacy Policy needs a real,
 * identifiable Data Fiduciary to be enforceable, and that identity isn't
 * something this document can invent on your behalf. Fill them in (and
 * have this reviewed by counsel) before treating this as final.
 */
export function PrivacyPolicy() {
  return (
    <article className="legal-doc">
      <h1>Privacy Policy</h1>
      <p className="legal-doc__meta">Effective {EFFECTIVE_DATE}</p>

      <p>
        This Privacy Policy explains how <strong>[YOUR REGISTERED BUSINESS / ENTITY NAME]</strong>{" "}
        ("we", "us", "the Company"), the operator of this support-automation platform (the
        "Service"), collects, uses, discloses, and protects personal data, in accordance with
        the Digital Personal Data Protection Act, 2023 ("DPDPA") and the Information Technology
        Act, 2000 and its associated rules. By using the Service, you agree to the practices
        described here.
      </p>

      <h2>1. Who this policy covers, and two different roles</h2>
      <p>
        When you sign in and configure the Service, we act as the <strong>Data Fiduciary</strong>{" "}
        for your account information (your name, email, and authentication details). When your
        organization connects a case system (Salesforce, HubSpot, etc.) and the Service processes
        your customers' support cases on your behalf, we act as a <strong>Data Processor</strong> —
        that data remains yours, and you remain the Data Fiduciary responsible to your own
        customers for it. This policy addresses both, and is explicit about which is which below.
      </p>

      <h2>2. What personal data we collect</h2>
      <ul>
        <li>
          <strong>Account data:</strong> name, email address, and profile information from your
          chosen sign-in method (email/password, magic link, or Google OAuth).
        </li>
        <li>
          <strong>Workspace data:</strong> the flows, knowledge sources, rules, and connection
          configuration you create.
        </li>
        <li>
          <strong>Case data (as a Processor, on your behalf):</strong> the content of cases/tickets
          your connected case system sends the Service — sender details, message text, and any
          attachments — for as long as needed to process and respond to that case.
        </li>
        <li>
          <strong>Usage and log data:</strong> IP address, device/browser information, and
          timestamps of actions taken in the Service, kept for security, audit, and
          troubleshooting.
        </li>
      </ul>

      <h2>3. How we use personal data</h2>
      <ul>
        <li>To provide and operate the Service — authenticate you, run your flows, store your configuration.</li>
        <li>To draft replies: relevant case text is sent to the AI model provider configured for that step, only for the purpose of generating that reply.</li>
        <li>To maintain security — detect abuse, enforce rate limits, and investigate incidents.</li>
        <li>To provide support when you contact us.</li>
        <li>To comply with legal obligations.</li>
      </ul>

      <h2>4. Consent and legal basis</h2>
      <p>
        We collect account data with your consent at sign-up, for the specific purpose of
        providing the Service. You may withdraw consent at any time by closing your account,
        subject to data we're required to retain by law. Certain processing (security logging,
        fraud prevention) is carried out on the basis of legitimate use as permitted under the
        DPDPA, independent of consent, and is limited to what's necessary for that purpose.
      </p>

      <h2>5. Who we share data with</h2>
      <p>We do not sell personal data. We share it only with the following categories of processor, each bound to process data only as we instruct:</p>
      <ul>
        <li><strong>Infrastructure:</strong> Supabase (database, authentication, file storage).</li>
        <li><strong>AI model providers:</strong> Groq, Anthropic, and/or OpenRouter, depending on which model a given flow step is configured to use — only the specific case text relevant to that step is sent, not your entire workspace.</li>
        <li><strong>Connectors you configure:</strong> Salesforce, HubSpot, Slack, or any other integration you explicitly connect — data flows there because you set up that connection, and is governed additionally by that provider's own terms.</li>
        <li><strong>Legal disclosure:</strong> where required by law, court order, or to protect the rights, safety, or property of the Company or others.</li>
      </ul>

      <h2>6. Cross-border data transfer</h2>
      <p>
        Some of the processors above (in particular, AI model providers) may process data on
        servers located outside India. Where this occurs, we take reasonable steps to require
        the processor to protect the data to a standard consistent with this policy and
        applicable law. The DPDPA permits cross-border transfer except to countries specifically
        restricted by the Central Government; we do not transfer personal data to any such
        restricted destination.
      </p>

      <h2>7. How we protect your data</h2>
      <p>The Service is built with the following technical safeguards:</p>
      <ul>
        <li><strong>Tenant isolation:</strong> each workspace's data is isolated at the database level using PostgreSQL Row-Level Security — one workspace's data is not queryable from another's session.</li>
        <li><strong>Encryption in transit:</strong> all traffic to and from the Service is encrypted (TLS/HTTPS).</li>
        <li><strong>Encryption at rest for credentials:</strong> connected-integration credentials (e.g. your Salesforce/Slack tokens) are stored encrypted at rest, not in plain text.</li>
        <li><strong>Access control:</strong> role-based permissions within a workspace, and every request is authenticated against a verified session token.</li>
        <li><strong>Rate limiting and audit logging:</strong> abnormal request patterns are throttled, and security-relevant actions are logged.</li>
      </ul>
      <p className="muted">
        No system is perfectly secure, and we can't guarantee absolute security — but the
        measures above are real, current practices, not marketing language.
      </p>

      <h2>8. Data retention</h2>
      <p>
        We retain account and workspace data for as long as your workspace remains active, plus
        a reasonable period after closure to allow recovery of accidental deletion and to meet
        legal obligations. Case data is retained per your own configuration and can be deleted on
        request, subject to what your organization independently needs to retain in its own case
        system.
      </p>

      <h2>9. Your rights under the DPDPA</h2>
      <p>As a Data Principal, you have the right to:</p>
      <ul>
        <li>obtain a summary of the personal data we hold about you and how it's processed;</li>
        <li>request correction or completion of inaccurate or incomplete personal data;</li>
        <li>request erasure of personal data that's no longer necessary for the purpose it was collected;</li>
        <li>withdraw consent at any time, as easily as it was given;</li>
        <li>nominate another individual to exercise these rights on your behalf in the event of death or incapacity; and</li>
        <li>register a grievance with us, and if unresolved, with the Data Protection Board of India.</li>
      </ul>
      <p>To exercise any of these rights, contact us at {CONTACT_EMAIL}.</p>

      <h2>10. Children's data</h2>
      <p>The Service is intended for business use and is not directed at, or knowingly used to collect data from, individuals under 18 years of age.</p>

      <h2>11. Grievance Officer</h2>
      <p>
        In accordance with the Information Technology Act, 2000 and the DPDPA, the Grievance
        Officer for this Service is:
      </p>
      <p className="legal-doc__block">
        Name: <strong>[GRIEVANCE OFFICER NAME]</strong>
        <br />
        Email: {CONTACT_EMAIL}
        <br />
        Address: <strong>[YOUR REGISTERED ADDRESS]</strong>
      </p>
      <p>We aim to acknowledge grievances promptly and resolve them within the timelines required by applicable law.</p>

      <h2>12. Changes to this policy</h2>
      <p>We may update this policy from time to time. Material changes will be reflected by updating the effective date above; continued use of the Service after a change constitutes acceptance of the updated policy.</p>

      <h2>13. Contact us</h2>
      <p>
        Questions about this policy, or to exercise your rights under the DPDPA, contact:{" "}
        <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
      </p>
    </article>
  );
}

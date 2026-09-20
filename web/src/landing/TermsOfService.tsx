const CONTACT_EMAIL = "gundamvishnu7@gmail.com";
const EFFECTIVE_DATE = "17 September 2026";

/**
 * Same placeholder convention as PrivacyPolicy.tsx — entity name and the
 * jurisdiction for section 12 are bracketed and need a real answer before
 * this is final. Everything else is substantive, not filler.
 */
export function TermsOfService() {
  return (
    <article className="legal-doc">
      <h1>Terms of Service</h1>
      <p className="legal-doc__meta">Effective {EFFECTIVE_DATE}</p>

      <p>
        These Terms of Service ("Terms") govern your use of this support-automation platform (the
        "Service"), operated by <strong>[YOUR REGISTERED BUSINESS / ENTITY NAME]</strong> ("we",
        "us"). By creating an account or using the Service, you agree to these Terms.
      </p>

      <h2>1. The Service</h2>
      <p>
        The Service lets you build automated flows that read incoming support cases, draft
        replies grounded in your own connected knowledge sources, and route cases to a person
        when a case needs one. It is provided on a workspace basis — each organization's data and
        configuration is isolated to its own workspace.
      </p>

      <h2>2. Accounts and eligibility</h2>
      <ul>
        <li>You must be at least 18 years old and capable of entering a binding agreement to use the Service.</li>
        <li>If you're using the Service on behalf of an organization, you confirm you're authorized to bind that organization to these Terms.</li>
        <li>You're responsible for keeping your account credentials secure and for all activity under your account.</li>
      </ul>

      <h2>3. Acceptable use</h2>
      <p>You agree not to:</p>
      <ul>
        <li>use the Service for any unlawful purpose, or to process data you don't have the right to process;</li>
        <li>attempt to bypass, disable, or interfere with the Service's security, rate limits, or tenant isolation;</li>
        <li>reverse-engineer or attempt to extract the Service's underlying models or source code beyond what's provided;</li>
        <li>use the Service to generate or send content that is unlawful, defamatory, or fraudulent.</li>
      </ul>

      <h2>4. Your data</h2>
      <p>
        You retain ownership of the data you connect to or create in the Service, including your
        knowledge sources, flow configuration, and the case data your connected systems send.
        We process that data only to provide the Service to you, as described in our{" "}
        <strong>Privacy Policy</strong>. You're responsible for having the right to share any
        customer or case data you connect.
      </p>

      <h2>5. AI-generated content</h2>
      <p>
        Some replies and decisions in the Service are generated or assisted by AI models. While
        the Service is designed to ground drafts in your own knowledge base and to route
        low-confidence or high-risk cases to a person rather than auto-sending them, AI-generated
        content can still be incorrect. You're responsible for configuring your flows' confidence
        thresholds and escalation rules appropriately for your use case, and for reviewing
        auto-sent content where accuracy is critical.
      </p>

      <h2>6. Third-party integrations</h2>
      <p>
        The Service can connect to third-party systems (e.g. Salesforce, HubSpot, Slack) and AI
        model providers (e.g. Groq, Anthropic, OpenRouter) that you choose to enable. Your use of
        those integrations is also subject to that provider's own terms; we aren't responsible
        for their availability or conduct.
      </p>

      <h2>7. Fees and billing</h2>
      <p>
        Any fees for the Service are as described in your plan at the time of signup, billed on
        the cycle stated there. Fees are non-refundable except as required by law or as we
        expressly agree in writing.
      </p>

      <h2>8. Termination</h2>
      <p>
        You may stop using the Service and close your account at any time. We may suspend or
        terminate access if you materially breach these Terms, including the acceptable-use
        section above, after reasonable notice where practicable.
      </p>

      <h2>9. Disclaimer of warranties</h2>
      <p>
        The Service is provided "as is" and "as available." To the maximum extent permitted by
        law, we disclaim all warranties, express or implied, including fitness for a particular
        purpose and non-infringement. We don't warrant that the Service, or any AI-generated
        output, will be uninterrupted, error-free, or fully accurate.
      </p>

      <h2>10. Limitation of liability</h2>
      <p>
        To the maximum extent permitted by law, we won't be liable for indirect, incidental, or
        consequential damages arising from your use of the Service, including damages resulting
        from AI-generated content sent to your customers. Nothing in these Terms limits liability
        that cannot be limited under applicable Indian law.
      </p>

      <h2>11. Indemnification</h2>
      <p>
        You agree to indemnify us against claims arising from your use of the Service in
        violation of these Terms, or from data you connect to it that you didn't have the right
        to share.
      </p>

      <h2>12. Governing law and jurisdiction</h2>
      <p>
        These Terms are governed by the laws of India. Subject to applicable law, the courts of{" "}
        <strong>[YOUR CITY OF JURISDICTION]</strong> shall have exclusive jurisdiction over any
        dispute arising from these Terms or the Service.
      </p>

      <h2>13. Changes to these Terms</h2>
      <p>
        We may update these Terms from time to time. Material changes will be reflected by
        updating the effective date above; continued use of the Service after a change
        constitutes acceptance of the updated Terms.
      </p>

      <h2>14. Contact us</h2>
      <p>
        Questions about these Terms: <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
      </p>
    </article>
  );
}

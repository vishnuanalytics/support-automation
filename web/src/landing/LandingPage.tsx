import { Button } from "../ui";

type Feature = { title: string; body: string };

const FEATURES: Feature[] = [
  {
    title: "Build flows without writing code",
    body: "A visual canvas, plain-English AI edits, and a guided setup wizard — conditions are picked from real dropdowns, not typed as expressions.",
  },
  {
    title: "Answers grounded in your own knowledge",
    body: "Every drafted reply is grounded in your connected docs and past resolved cases — not a model guessing from general knowledge.",
  },
  {
    title: "Knows when to ask a person",
    body: "A confidence gate and your own policy rules decide, per case, whether to auto-reply, ask a teammate, or hand off — never a blind guess.",
  },
  {
    title: "Works with the tools you already use",
    body: "Salesforce, HubSpot, Slack, email, and webhooks — connect once, and one flow per team routes real work through them.",
  },
  {
    title: "See exactly why, every time",
    body: "Every run keeps a full step-by-step trace — what it read, what it decided, and why — for audit and for debugging.",
  },
  {
    title: "Built for teams, securely",
    body: "Each workspace's data is isolated at the database level, connected-account credentials are encrypted at rest, and access is role-based.",
  },
];

export function LandingPage({ onSignIn }: { onSignIn: () => void }) {
  return (
    <div className="landing">
      <section className="landing-hero">
        <span className="landing-hero__kicker">No-code support automation</span>
        <h1 className="landing-hero__title">Support automation that knows when to ask for help.</h1>
        <p className="landing-hero__lede">
          Build flows that read every incoming case, draft a reply grounded in your own
          knowledge base, and escalate to a person instead of guessing — all without writing
          code.
        </p>
        <div className="row" style={{ gap: 10 }}>
          <Button variant="primary" onClick={onSignIn}>
            Sign in to get started
          </Button>
        </div>
      </section>

      <section className="landing-features">
        {FEATURES.map((f) => (
          <div className="landing-feature" key={f.title}>
            <div className="landing-feature__title">{f.title}</div>
            <div className="landing-feature__body">{f.body}</div>
          </div>
        ))}
      </section>

      <section className="landing-cta">
        <div className="landing-cta__title">Ready to see it in action?</div>
        <p className="muted" style={{ margin: "6px 0 16px", maxWidth: 480 }}>
          Sign in with your work email or Google account — your workspace's flows, knowledge
          base, and connections are set up from there.
        </p>
        <Button variant="primary" onClick={onSignIn}>
          Sign in
        </Button>
      </section>
    </div>
  );
}

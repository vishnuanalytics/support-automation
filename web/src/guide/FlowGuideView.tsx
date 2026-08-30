/**
 * "Flow Guide" — a faithful port of the Claude Design broadsheet
 * `Support Automation Flow.dc.html`: an editorial explainer of how an
 * inbound email becomes a handled Salesforce Case. Read-only reference
 * content; self-contained styling (serif / paper) so it doesn't inherit
 * the app's dark editor chrome.
 */

const INK = "#201e1d";
const PAPER = "#f3f2f2";
const FILL = "#eae9e9";
const CYAN = "#0088b0";
const CYAN_D = "#006786";
const CYAN_DD = "#004961";
const CYAN_TINT = "#e9f8ff";
const MAGENTA = "#d6006c";

const serif = "'Source Serif 4', Georgia, 'Times New Roman', serif";

type Stage = { n: number; node: string; title: string; body: string; hasArrow: boolean };
type Team = { name: string; desc: string };
type TraceStep = { n: number; label: string; detail: string };
type NodeRef = { type: string; desc: string };

const STAGES: Stage[] = [
  { n: 1, node: "identify", title: "Identify the sender", body: "Match the from-address to a known Contact, or its domain to a known Account. Unrecognized senders are logged, not auto-created as a Lead.", hasArrow: true },
  { n: 2, node: "sf_case", title: "Create or reuse the Salesforce Case", body: 'Origin "Email", status "New". Reuses an open Case from the same contact if one exists within the last 14 days; otherwise creates Contact, Account, and Case as needed.', hasArrow: true },
  { n: 3, node: "retrieve", title: "Retrieve knowledge-base context", body: "Hybrid dense + sparse search over docs synced from Google Drive and the product docs, expanded via a Neo4j graph and re-ranked; top 5 chunks kept.", hasArrow: true },
  { n: 4, node: "classify", title: "Classify tier, topic, urgency", body: "Tier comes from the Account’s customer type (basic / premium / enterprise); a brand-new Account defaults to basic so first-time senders can still get an auto-reply.", hasArrow: true },
  { n: 5, node: "sf_writeback", title: "Write triage fields back to the Case", body: "Priority, Module__c, Region__c, and a summary appended to Description — so anyone opening the Case in Salesforce sees the triage, not just the raw email.", hasArrow: true },
  { n: 6, node: "draft", title: "Draft a reply", body: "The model drafts a reply grounded in the retrieved context, with its own confidence score attached.", hasArrow: true },
  { n: 7, node: "confidence_gate", title: "Gate on answer quality", body: "A weighted score (55% retrieval match, 35% groundedness, 10% draft confidence) is checked against a bar that rises with customer tier — basic 0.5, premium 0.6, enterprise 0.75. Billing, refund, pricing, legal, and a handful of other topics always fail the gate, whatever the score.", hasArrow: true },
  { n: 8, node: "outcome", title: "Route the outcome", body: "Auto-reply, ask a human, or hand over — see below.", hasArrow: false },
];

const TEAMS: Team[] = [
  { name: "email", desc: "The inbound mailbox poller — the flow described above, end to end." },
  { name: "support", desc: "Cases opened directly in Salesforce rather than by email." },
  { name: "csm", desc: "Account-management and renewal questions from customer success." },
  { name: "sales", desc: "Pre-sales and pricing questions routed away from support." },
  { name: "offboarding", desc: "Cancellations and data-export requests — requests to export data older than two years get an extra step: a GitHub ops ticket, gated on a lead’s approval in Slack." },
];

const TRACE: TraceStep[] = [
  { n: 1, label: "Identify", detail: "priya@northwind.example matches an existing Contact on Northwind Ltd." },
  { n: 2, label: "Case", detail: 'Reuses no open Case; creates Case "Unexpected charge after upgrading my plan".' },
  { n: 3, label: "Retrieve", detail: "Pulls billing/proration docs from the knowledge base." },
  { n: 4, label: "Classify", detail: "Tier: premium. Topic: billing. Region: EMEA." },
  { n: 5, label: "Write back", detail: "Priority and Module__c written to the Case." },
  { n: 6, label: "Draft", detail: "Bot drafts an explanation of prorated billing, confidence 0.71." },
  { n: 7, label: "Gate", detail: '"billing" is a forced-escalation topic — fails the gate regardless of score.' },
  { n: 8, label: "Ask human", detail: "Posts to the Case in Chatter. A billing specialist replies in a comment; that reply is read back and sent to Priya as the resolution." },
];

const NODE_REF: NodeRef[] = [
  { type: "identify", desc: "Resolves the sender to a Contact, Lead, or known domain." },
  { type: "sf_case", desc: "Creates or reuses the Salesforce Case for this thread." },
  { type: "retrieve", desc: "Hybrid search + rerank over the knowledge base." },
  { type: "kb_lookup", desc: "Looks up an internal-only knowledge collection (used by some team flows in place of, or alongside, retrieve)." },
  { type: "classify", desc: "Derives tier, topic, region, urgency from the case and Account." },
  { type: "extract", desc: 'Pulls named fields out of the case body for policy rules to key on (e.g. "how old is the data").' },
  { type: "sf_writeback", desc: "Writes triage fields (Priority, Module__c, Region__c, Description) to the Case." },
  { type: "draft", desc: "Drafts the reply text and a self-reported confidence." },
  { type: "confidence_gate", desc: "Scores the draft and compares it to a per-tier bar; some topics always fail it." },
  { type: "policy_gate", desc: "Checks the case against structured if/then rules (e.g. old-data-export → ops ticket)." },
  { type: "task_dispatch", desc: "Fires an internal task from a matched policy rule (e.g. opens a GitHub issue)." },
  { type: "clarify", desc: "Asks the customer directly for missing details, instead of escalating, when the topic is benign." },
  { type: "auto_reply", desc: "Sends the drafted reply straight to the customer." },
  { type: "ask_human", desc: "Posts to the Case in Chatter, @mentioning a person or queue." },
  { type: "handover", desc: "Routes the case to a full human handover, bypassing the bot entirely." },
];

const h2: React.CSSProperties = { fontSize: 28, margin: "0 0 6px", fontWeight: 600, letterSpacing: "-0.01em" };
const h2Later: React.CSSProperties = { ...h2, margin: "56px 0 6px" };
const lede: React.CSSProperties = { fontSize: 14, color: "rgba(32,30,29,0.65)", margin: "0 0 24px", maxWidth: 660 };

function Kicker({ children, color = CYAN }: { children: React.ReactNode; color?: string }) {
  return (
    <div style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", color }}>{children}</div>
  );
}

function OutcomeCard({
  kicker, kickerColor, title, body, meta,
}: { kicker: string; kickerColor: string; title: string; body: string; meta: string }) {
  return (
    <div style={{ background: FILL, borderRadius: 2, padding: 20, display: "flex", flexDirection: "column", gap: 8 }}>
      <Kicker color={kickerColor}>{kicker}</Kicker>
      <div style={{ fontFamily: serif, fontWeight: 600, fontSize: 17 }}>{title}</div>
      <div style={{ fontSize: 13, lineHeight: 1.55, opacity: 0.8, flex: 1 }}>{body}</div>
      <div style={{ fontSize: 11, color: "rgba(32,30,29,0.5)" }}>{meta}</div>
    </div>
  );
}

export function FlowGuideView() {
  return (
    <div style={{ height: "100%", overflow: "auto", background: PAPER }}>
      <div style={{ background: PAPER, color: INK, fontFamily: serif, minHeight: "100%" }}>
        <div style={{ maxWidth: 880, margin: "0 auto", padding: "60px 24px 100px" }}>

          {/* Masthead */}
          <div
            style={{
              borderBottom: `2px solid ${INK}`, paddingBottom: 14, marginBottom: 8,
              display: "flex", alignItems: "baseline", justifyContent: "space-between",
              gap: 16, flexWrap: "wrap",
            }}
          >
            <div style={{ fontSize: 11, letterSpacing: "0.12em", textTransform: "uppercase", color: CYAN }}>
              Support Automation — Internal Brief
            </div>
            <div style={{ fontSize: 11, letterSpacing: "0.05em", color: "rgba(32,30,29,0.55)" }}>August 30, 2026</div>
          </div>
          <h1 style={{ fontSize: 44, lineHeight: 1.08, margin: "18px 0 10px", letterSpacing: "-0.015em", fontWeight: 600 }}>
            Email to Salesforce: how a case gets handled
          </h1>
          <p style={{ fontSize: 17, lineHeight: 1.55, color: "rgba(32,30,29,0.75)", margin: "0 0 36px", maxWidth: 640 }}>
            An inbound email becomes a Salesforce Case, gets triaged by an automated agent, and either gets
            answered, escalated to a person, or handed to the right team — depending on who the customer is
            and how confident the agent is in its own answer. This is the actual shape of the{" "}
            <span style={{ fontStyle: "italic" }}>support-automation</span> flow engine, not a proposal.
          </p>

          {/* Pipeline */}
          <h2 style={h2}>The pipeline</h2>
          <p style={{ ...lede, maxWidth: 600, margin: "0 0 28px" }}>
            Eight steps, run in order for every inbound email on the <span style={{ color: CYAN_D }}>email</span> team&rsquo;s flow.
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
            {STAGES.map((stage) => (
              <div key={stage.n} style={{ display: "flex", flexDirection: "column" }}>
                <div style={{ display: "flex", gap: 20, alignItems: "flex-start", background: FILL, borderRadius: 2, padding: "18px 20px" }}>
                  <div style={{ fontFamily: serif, fontWeight: 600, fontSize: 22, color: CYAN, minWidth: 28, flex: "none" }}>
                    {stage.n}
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 4 }}>
                      <div style={{ fontFamily: serif, fontWeight: 600, fontSize: 17 }}>{stage.title}</div>
                      <div style={{ display: "inline-flex", fontSize: 10, letterSpacing: "0.06em", padding: "2px 8px", borderRadius: 2, background: CYAN_TINT, color: CYAN_DD }}>
                        {stage.node}
                      </div>
                    </div>
                    <div style={{ fontSize: 14, lineHeight: 1.6, color: "rgba(32,30,29,0.8)" }}>{stage.body}</div>
                  </div>
                </div>
                {stage.hasArrow && (
                  <div style={{ display: "flex", justifyContent: "flex-start", paddingLeft: 32, height: 20, alignItems: "center", color: "rgba(32,30,29,0.35)", fontSize: 13 }}>
                    ↓
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Three outcomes */}
          <h2 style={h2Later}>Three ways a case ends</h2>
          <p style={lede}>
            The confidence gate scores each draft, compares it to a per-tier bar, and routes to exactly one of these.
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16 }}>
            <OutcomeCard
              kicker="Auto-reply" kickerColor={CYAN} title="Bot sends the email"
              body="Gate passes and tier isn't enterprise. The drafted reply goes straight to the customer."
              meta="Threshold: basic 0.5 · premium 0.6"
            />
            <OutcomeCard
              kicker="Ask human" kickerColor={MAGENTA} title="Post to the Case"
              body="Gate fails, or the topic is billing / refund / legal / cancellation and always needs a person. A Chatter post goes on the Case, @mentioning the assigned person or queue."
              meta="Channel: salesforce_chatter"
            />
            <OutcomeCard
              kicker="Handover" kickerColor={MAGENTA} title="Full handover"
              body="Enterprise accounts always land here, regardless of confidence — the highest-value customers never get an unreviewed reply."
              meta="Reason: enterprise_tier"
            />
          </div>

          {/* Doubt vs dead end */}
          <h2 style={h2Later}>When the bot has doubts</h2>
          <p style={lede}>
            Not every escalation is the same. The system tells apart a case it&rsquo;s unsure about from one it
            has no basis to answer at all.
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 32 }}>
            <div>
              <div style={{ fontFamily: serif, fontStyle: "italic", fontSize: 19, color: CYAN_D, marginBottom: 10 }}>
                Doubt — ask, then answer
              </div>
              <div style={{ fontSize: 14, lineHeight: 1.65, opacity: 0.85 }}>
                The draft is below the confidence bar, or it touches a topic that&rsquo;s never auto-sent (billing,
                pricing, legal). The bot posts the question on the Case in Chatter. A teammate replies in a
                comment or by email on that Case. The system reads that reply back — comparing it to what the
                bot would have sent — and that becomes the answer that goes to the customer.
              </div>
            </div>
            <div>
              <div style={{ fontFamily: serif, fontStyle: "italic", fontSize: 19, color: MAGENTA, marginBottom: 10 }}>
                Dead end — ask, then hand off
              </div>
              <div style={{ fontSize: 14, lineHeight: 1.65, opacity: 0.85 }}>
                The customer is enterprise-tier, or the case falls outside what any team&rsquo;s flow is built to
                touch. The bot still posts to the Case so the thread isn&rsquo;t silent, but the case itself moves
                to a full handover — routed to the queue for the team whose flow owns that kind of case, not
                answered by the bot at all.
              </div>
            </div>
          </div>

          {/* Teams */}
          <h2 style={h2Later}>One team, one flow</h2>
          <p style={lede}>
            Each team publishes its own version of this flow — same shape, different rules for what counts as
            &ldquo;confident enough.&rdquo;
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
            {TEAMS.map((team) => (
              <div key={team.name} style={{ display: "flex", gap: 16, alignItems: "baseline", padding: "14px 0", borderBottom: "1px solid rgba(32,30,29,0.1)" }}>
                <div style={{ minWidth: 110, flex: "none", fontFamily: serif, fontWeight: 600, fontSize: 14, color: CYAN }}>
                  {team.name}
                </div>
                <div style={{ fontSize: 14, lineHeight: 1.55, opacity: 0.85 }}>{team.desc}</div>
              </div>
            ))}
          </div>

          {/* Worked example */}
          <h2 style={h2Later}>Worked example</h2>
          <p style={{ ...lede, margin: "0 0 4px" }}>One real case, followed step by step through the &ldquo;doubt&rdquo; path.</p>
          <div style={{ fontSize: 13, fontStyle: "italic", opacity: 0.7, marginBottom: 24 }}>
            &ldquo;Unexpected charge after upgrading my plan&rdquo; — Priya Nair, Northwind Ltd (premium, EMEA)
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
            {TRACE.map((step) => (
              <div key={step.n} style={{ display: "flex", gap: 16, padding: "10px 0" }}>
                <div style={{ width: 20, flex: "none", textAlign: "right", fontSize: 12, color: "rgba(32,30,29,0.4)", paddingTop: 2 }}>
                  {step.n}
                </div>
                <div style={{ flex: 1, fontSize: 14, lineHeight: 1.6 }}>
                  <span style={{ fontWeight: 600 }}>{step.label}</span> — {step.detail}
                </div>
              </div>
            ))}
          </div>

          {/* Appendix */}
          <h2 style={h2Later}>Appendix — node reference</h2>
          <p style={{ ...lede, margin: "0 0 24px" }}>Every node type in use across the five team flows.</p>
          <div style={{ display: "table", width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
            <div style={{ display: "table-row" }}>
              {["Node", "What it does"].map((head) => (
                <div key={head} style={{ display: "table-cell", textAlign: "left", fontSize: 11, letterSpacing: "0.08em", textTransform: "uppercase", color: "rgba(32,30,29,0.6)", padding: 10, borderBottom: "1px solid rgba(32,30,29,0.16)" }}>
                  {head}
                </div>
              ))}
            </div>
            {NODE_REF.map((row) => (
              <div key={row.type} style={{ display: "table-row" }}>
                <div style={{ display: "table-cell", padding: 10, borderBottom: "1px solid rgba(32,30,29,0.08)", fontWeight: 600, whiteSpace: "nowrap", verticalAlign: "top" }}>
                  {row.type}
                </div>
                <div style={{ display: "table-cell", padding: 10, borderBottom: "1px solid rgba(32,30,29,0.08)", opacity: 0.85, verticalAlign: "top" }}>
                  {row.desc}
                </div>
              </div>
            ))}
          </div>

          <div style={{ marginTop: 48, paddingTop: 16, borderTop: "1px solid rgba(32,30,29,0.16)", fontSize: 11, color: "rgba(32,30,29,0.5)" }}>
            Source: vishnuanalytics/support-automation, branch main. Knowledge base is synced from Google Drive
            and Zapier&rsquo;s public docs into Supabase + Neo4j; the &ldquo;automation engine&rdquo; is this
            repo&rsquo;s interpreter, reading flow rules stored as data rather than code.
          </div>

        </div>
      </div>
    </div>
  );
}

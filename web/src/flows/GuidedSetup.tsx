import { useState } from "react";
import { api, ApiError } from "../api";
import type { FlowCandidate, FlowNode } from "../types";
import { Banner, Button, Field, SlideOver } from "../ui";

/**
 * "Answer a few questions" flow creation — for someone who'd find a blank
 * canvas, a free-text AI prompt, or a raw template picker all equally
 * intimidating. Two steps: pick the goal (maps 1:1 to one of the 4
 * built-in templates — a user's own custom templates aren't offered here,
 * since their shape isn't known ahead of time), then answer up to 3 short
 * questions *adapted to whichever tunable nodes that template actually
 * has* (a webhook Q&A flow has no confidence gate to ask about). Builds on
 * the exact same `FlowCandidate` + `pendingCandidate` sessionStorage
 * hand-off every other creation path (From template / From prompt / From
 * Mermaid) already uses — this is a friendlier front end for the template
 * picker, not a new persistence path. Custom templates, the AI, and the
 * raw canvas are all still there for anyone who wants them.
 */

type Intent = { templateId: string; label: string; description: string };

const INTENTS: Intent[] = [
  {
    templateId: "support-autoreply",
    label: "Answer common questions automatically",
    description: "Looks up your knowledge base and replies on its own when it's confident — otherwise hands the case to a person.",
  },
  {
    templateId: "triage-and-route",
    label: "Sort incoming cases and send them to the right team",
    description: "Reads each case, decides what it's about, and hands it to the team that should own it.",
  },
  {
    templateId: "draft-then-approve-in-slack",
    label: "Draft replies, but always have a person approve first",
    description: "Nothing ever goes to the customer automatically — someone reviews and sends every reply in Slack.",
  },
  {
    templateId: "webhook-rag-qa",
    label: "Answer questions from an app or website",
    description: "No case system involved — a question comes in, an answer grounded in your docs goes out.",
  },
];

type Tunable = {
  nodeType: FlowNode["type"];
  configKey: string;
  question: string;
  kind: "select" | "text";
  options?: { value: string; label: string }[];
  placeholder?: string;
};

const TUNABLES: Tunable[] = [
  {
    nodeType: "confidence_gate",
    configKey: "default_threshold",
    question: "How confident should it be before acting on its own?",
    kind: "select",
    options: [
      { value: "0.5", label: "Cautious — send more to a person" },
      { value: "0.35", label: "Balanced (recommended)" },
      { value: "0.2", label: "Confident — act on its own more often" },
    ],
  },
  {
    nodeType: "ask_human",
    configKey: "queue",
    question: "Which queue should low-confidence cases go to?",
    kind: "text",
    placeholder: "e.g. Support Queue (blank = your default queue)",
  },
  {
    nodeType: "handover",
    configKey: "queue",
    question: "Which queue should cases hand off to?",
    kind: "text",
    placeholder: "e.g. Support Queue (blank = your default queue)",
  },
  {
    nodeType: "notify_human",
    configKey: "slack_channel",
    question: "Which Slack channel should review drafts?",
    kind: "text",
    placeholder: "#support-drafts (blank = pick this later)",
  },
];

export function GuidedSetup({
  open,
  onClose,
  onGenerate,
}: {
  open: boolean;
  onClose: () => void;
  /** a ready-to-review FlowCandidate + a friendly suggested flow name */
  onGenerate: (candidate: FlowCandidate, suggestedName: string) => void;
}) {
  const [step, setStep] = useState<1 | 2>(1);
  const [intent, setIntent] = useState<Intent | null>(null);
  const [cand, setCand] = useState<FlowCandidate | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function reset() {
    setStep(1);
    setIntent(null);
    setCand(null);
    setAnswers({});
    setErr(null);
  }

  async function pickIntent(picked: Intent) {
    setBusy(true);
    setErr(null);
    try {
      const c = await api.templates.graph(picked.templateId);
      setIntent(picked);
      setCand(c);
      setAnswers({});
      setStep(2);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : (e as Error).message);
    }
    setBusy(false);
  }

  const applicable = cand
    ? TUNABLES.filter((t) => cand.nodes.some((n) => n.type === t.nodeType))
    : [];

  function finish() {
    if (!cand || !intent) return;
    const patched: FlowCandidate = {
      ...cand,
      nodes: cand.nodes.map((n) => {
        const t = applicable.find((x) => x.nodeType === n.type);
        const raw = t && answers[t.nodeType]?.trim();
        if (!t || !raw) return n;
        const value: unknown = t.kind === "select" ? Number(raw) : raw;
        return { ...n, config: { ...n.config, [t.configKey]: value } };
      }),
    };
    onGenerate(patched, cand.name || intent.label);
    reset();
  }

  return (
    <SlideOver
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title="Set up a flow"
      width={380}
      footer={
        step === 2 ? (
          <>
            <Button variant="ghost" onClick={() => setStep(1)}>
              « back
            </Button>
            <Button variant="primary" onClick={finish}>
              Create this flow
            </Button>
          </>
        ) : undefined
      }
    >
      {step === 1 && (
        <div className="col" style={{ gap: 8 }}>
          <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
            What should this flow do? Pick the closest match — you can change anything
            afterward.
          </p>
          {INTENTS.map((it) => (
            <button
              key={it.templateId}
              type="button"
              className="palette__item"
              style={{ flexDirection: "column", alignItems: "flex-start", height: "auto", padding: "10px 12px" }}
              onClick={() => pickIntent(it)}
              disabled={busy}
            >
              <span style={{ fontWeight: 600 }}>{it.label}</span>
              <span className="muted" style={{ fontSize: 11.5, fontWeight: 400 }}>{it.description}</span>
            </button>
          ))}
          {err && <Banner tone="exception" title={err} />}
        </div>
      )}

      {step === 2 && cand && (
        <div className="col" style={{ gap: 12 }}>
          <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
            {intent?.label} — a couple of quick questions, then it's ready to review.
          </p>
          {applicable.map((t) => (
            <Field label={t.question} key={t.nodeType}>
              {t.kind === "select" ? (
                <select
                  value={answers[t.nodeType] ?? ""}
                  onChange={(e) => setAnswers((a) => ({ ...a, [t.nodeType]: e.target.value }))}
                >
                  <option value="">recommended default</option>
                  {t.options!.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              ) : (
                <input
                  value={answers[t.nodeType] ?? ""}
                  placeholder={t.placeholder}
                  onChange={(e) => setAnswers((a) => ({ ...a, [t.nodeType]: e.target.value }))}
                />
              )}
            </Field>
          ))}
          {applicable.length === 0 && (
            <p className="muted" style={{ fontSize: 12.5 }}>
              Nothing to fine-tune for this one yet — you'll be able to adjust details
              once it's created.
            </p>
          )}
        </div>
      )}
    </SlideOver>
  );
}

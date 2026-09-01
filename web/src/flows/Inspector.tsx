import { useEffect, useState } from "react";
import { api } from "../api";
import type { KbCollection, SfMeta } from "../types";
import type { RFEdge, RFNode } from "./graph";

// Salesforce routing metadata (queues + Case.Type / Module picklists) — fetched
// once per editor session, shared by the notify / clarify forms.
const _EMPTY_META: SfMeta = { available: false, queues: [], case_types: [], modules: [] };
let _metaCache: SfMeta | null = null;
let _metaPromise: Promise<SfMeta> | null = null;

function useSfMeta(): SfMeta {
  const [meta, setMeta] = useState<SfMeta>(_metaCache ?? _EMPTY_META);
  useEffect(() => {
    if (_metaCache) return;
    _metaPromise =
      _metaPromise ||
      api.salesforce.meta().catch(() => _EMPTY_META);
    _metaPromise.then((m) => {
      _metaCache = m;
      setMeta(m);
    });
  }, []);
  return meta;
}

/** A Salesforce-Queue picker: a <select> of the org's queues when the API
 *  could reach Salesforce, otherwise a plain text box. Keeps the current
 *  value even if it is not (yet) in the list. */
function QueuePicker({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const meta = useSfMeta();
  if (!meta.available || meta.queues.length === 0) {
    return (
      <input
        value={value}
        placeholder={placeholder || "queue DeveloperName"}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  const names = meta.queues.map((q) => q.developer_name || q.name);
  const known = value === "" || names.includes(value);
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">— none —</option>
      {!known && <option value={value}>{value} (not in org)</option>}
      {meta.queues.map((q) => (
        <option key={q.id} value={q.developer_name || q.name}>
          {q.name}
        </option>
      ))}
    </select>
  );
}

export function NodeInspector({
  node,
  config,
  onLabel,
  onConfig,
  onDelete,
}: {
  node: RFNode;
  config: Record<string, unknown>;
  onLabel: (v: string) => void;
  onConfig: (v: Record<string, unknown>) => void;
  onDelete: () => void;
}) {
  return (
    <div>
      <h4>
        <span className="muted">{node.data.nodeType}</span> node
      </h4>
      <div className="field">
        <label>label</label>
        <input value={node.data.label} onChange={(e) => onLabel(e.target.value)} />
      </div>

      {node.data.nodeType === "confidence_gate" && (
        <GateForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "kb_lookup" && (
        <KbLookupForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "extract" && (
        <ExtractForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "clarify" && (
        <ClarifyForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "notify" && (
        <NotifyForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "notify_human" && (
        <NotifyHumanForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "ai_prompt" && (
        <AiPromptForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "sf_context" && (
        <SfContextForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "attachments" && (
        <AttachmentsForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "identify" && (
        <IdentifyForm config={config} onConfig={onConfig} />
      )}

      {(node.data.nodeType === "policy_gate" || node.data.nodeType === "task_dispatch") && (
        <div className="muted" style={{ fontSize: 11, borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
          {node.data.nodeType === "policy_gate"
            ? "evaluates this team's rules (Rules tab) against the run; route on policy.action == 'ask_human' etc."
            : "raises the matched rule's task for Slack approval; wire it after policy_gate on policy.task != None"}
        </div>
      )}

      <JsonField label="config (jsonb)" value={config} onChange={onConfig} />

      <button className="err" onClick={onDelete}>
        delete node
      </button>
    </div>
  );
}

function GateForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const to = (config.tier_overrides as Record<string, number>) || {};
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  const setTier = (tier: string, v: number) =>
    set({ tier_overrides: { ...to, [tier]: v } });
  const num = (v: unknown, d = 0) => (typeof v === "number" ? v : d);

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <label>thresholds</label>
      <div className="row">
        <span className="muted" style={{ width: 90 }}>default</span>
        <input
          type="number" step="0.05" min="0" max="1"
          value={num(config.default_threshold, 0.35)}
          onChange={(e) => set({ default_threshold: parseFloat(e.target.value) })}
        />
      </div>
      {["basic", "premium", "enterprise"].map((t) => (
        <div className="row" key={t}>
          <span className="muted" style={{ width: 90 }}>{t}</span>
          <input
            type="number" step="0.05" min="0" max="1"
            value={num(to[t], 0.35)}
            onChange={(e) => setTier(t, parseFloat(e.target.value))}
          />
        </div>
      ))}
      <div className="row">
        <span className="muted" style={{ width: 90 }}>retr. weight</span>
        <input
          type="number" step="0.1" min="0" max="1"
          value={num(config.retrieval_weight, 0.5)}
          onChange={(e) => set({ retrieval_weight: parseFloat(e.target.value) })}
        />
      </div>
    </div>
  );
}

function ExtractForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const fields = (config.fields as Record<string, string>) || {};
  const rows = Object.entries(fields);
  const setFields = (f: Record<string, string>) => onConfig({ ...config, fields: f });

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <label>fields to extract into state.entities</label>
      {rows.map(([k, v], i) => (
        <div className="row" key={i} style={{ gap: 4 }}>
          <input
            value={k}
            placeholder="report_period_years"
            style={{ maxWidth: 150 }}
            onChange={(e) => {
              const next: Record<string, string> = {};
              rows.forEach(([kk, vv], j) => (next[j === i ? e.target.value : kk] = vv));
              setFields(next);
            }}
          />
          <input
            value={v}
            placeholder="how old are the requested reports, in years"
            onChange={(e) => setFields({ ...fields, [k]: e.target.value })}
          />
          <button
            className="err"
            onClick={() => {
              const next = { ...fields };
              delete next[k];
              setFields(next);
            }}
          >
            ×
          </button>
        </div>
      ))}
      <button onClick={() => setFields({ ...fields, "": "" })}>＋ field</button>
    </div>
  );
}

function ClarifyForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  const maxQ = typeof config.max_questions === "number" ? config.max_questions : 3;
  const maxRounds = typeof config.max_rounds === "number" ? config.max_rounds : 2;
  const autoSend = config.auto_send === true;
  const handoverQueue =
    typeof config.handover_queue === "string" ? config.handover_queue : "";

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        low-confidence recovery: asks the customer for the missing details
        (their reply comes back as a new case). Terminal.
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 110 }}>max questions</span>
        <input
          type="number" min="1" max="5"
          value={maxQ}
          onChange={(e) => set({ max_questions: parseInt(e.target.value, 10) || 1 })}
        />
      </div>
      <div className="row">
        <span className="muted" style={{ width: 110 }}>max rounds</span>
        <input
          type="number" min="1" max="5"
          value={maxRounds}
          onChange={(e) => set({ max_rounds: parseInt(e.target.value, 10) || 1 })}
        />
      </div>
      <div className="row">
        <span className="muted" style={{ width: 110 }}>channel</span>
        <input
          value={typeof config.channel === "string" ? config.channel : "email"}
          onChange={(e) => set({ channel: e.target.value })}
        />
      </div>
      <div className="row">
        <span className="muted" style={{ width: 110 }}>handover queue</span>
        <QueuePicker
          value={handoverQueue}
          placeholder="Team_Support"
          onChange={(v) => set({ handover_queue: v || undefined })}
        />
      </div>
      <div className="muted" style={{ fontSize: 11 }}>
        after <code>max rounds</code> of asking the customer, the Case is
        reassigned to this queue (blank = stay put, note only).
      </div>
      <label className="row" style={{ gap: 6, marginTop: 4 }}>
        <input
          type="checkbox"
          style={{ width: "auto" }}
          checked={autoSend}
          onChange={(e) => set({ auto_send: e.target.checked })}
        />
        auto-send to the customer
      </label>
      <div className="muted" style={{ fontSize: 11 }}>
        {autoSend
          ? "emails the questions to the customer (falls back to a public case comment); run is marked awaiting_customer."
          : "off: posts the questions to Chatter for an agent to send."}
      </div>
    </div>
  );
}

const CASE_TYPES_FALLBACK = [
  "Billing",
  "Account / Login",
  "Problem / Bug",
  "Feature Request",
  "How-to",
  "Question",
  "Other",
];

function NotifyForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const meta = useSfMeta();
  const caseTypes = meta.case_types.length ? meta.case_types : CASE_TYPES_FALLBACK;
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  const byType = (config.target_by_type as Record<string, string>) || {};
  const setTarget = (t: string, v: string) => {
    const next = { ...byType };
    if (v) next[t] = v;
    else delete next[t];
    set({ target_by_type: next });
  };

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        pings an internal rep on the Case <strong>without changing the owner</strong> —
        the Case stays in its current queue. Terminal; the resume poller
        re-engages the bot on the rep's CaseComment.
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        Targets normally come from the tenant <strong>notify_targets</strong> table
        (resolved from <code>Case.Type</code>, then <code>Module__c</code>, with a
        live Salesforce lookup for team / queue rows). Leave the rows below blank
        unless this flow needs an <em>override</em> for a specific Type.
      </div>
      <label style={{ marginTop: 6, display: "block" }}>
        override target by Case.Type{" "}
        {meta.available && <span className="muted">(picklist from Salesforce)</span>}
      </label>
      <div className="muted" style={{ fontSize: 11 }}>
        a Salesforce User / Group id (15–18 chars) → real @mention; any other
        text just names them in the note.
      </div>
      {caseTypes.map((t) => (
        <div className="row" key={t} style={{ gap: 4 }}>
          <span className="muted" style={{ width: 110 }}>{t}</span>
          <input
            value={byType[t] ?? ""}
            placeholder="User/Group id or name"
            onChange={(e) => setTarget(t, e.target.value.trim())}
          />
        </div>
      ))}
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 110 }}>fallback target</span>
        <input
          value={typeof config.fallback_target === "string" ? config.fallback_target : ""}
          placeholder="(optional)"
          onChange={(e) => set({ fallback_target: e.target.value.trim() || null })}
        />
      </div>
    </div>
  );
}

function AiPromptForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  const str = (k: string, d = "") => (typeof config[k] === "string" ? (config[k] as string) : d);
  const num = (k: string, d: number) => (typeof config[k] === "number" ? (config[k] as number) : d);
  const images = str("images", "none");

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        Runs one LLM call and writes the result to <code>state.{str("output_key", "ai_output")}</code>.
        Templates interpolate <code>{"{case.subject}"}</code>,{" "}
        <code>{"{sf_context.account.tier}"}</code>, <code>{"{attachment_text}"}</code>,{" "}
        <code>{"{classification.topic}"}</code> … Edges branch on the output; the
        routing stays a plain expression.
      </div>

      <label style={{ marginTop: 6, display: "block" }}>system prompt</label>
      <textarea rows={3} value={str("system")} onChange={(e) => set({ system: e.target.value })} />

      <label style={{ marginTop: 4, display: "block" }}>user prompt (template)</label>
      <textarea rows={4} value={str("user")} onChange={(e) => set({ user: e.target.value })} />

      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 90 }}>output key</span>
        <input value={str("output_key", "ai_output")}
               onChange={(e) => set({ output_key: e.target.value.trim() || "ai_output" })} />
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 90 }}>model</span>
        <input value={str("model", "openai/gpt-oss-120b")}
               onChange={(e) => set({ model: e.target.value.trim() })} />
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 90 }}>max tokens</span>
        <input type="number" value={num("max_tokens", 600)}
               onChange={(e) => set({ max_tokens: Number(e.target.value) || 600 })} />
        <span className="muted" style={{ width: 44, marginLeft: 8 }}>temp</span>
        <input type="number" step={0.1} min={0} max={1} value={num("temperature", 0.2)}
               onChange={(e) => set({ temperature: Number(e.target.value) })} />
      </div>

      <label style={{ marginTop: 6, display: "block" }}>images (vision)</label>
      <select value={images} onChange={(e) => set({ images: e.target.value })}>
        <option value="none">none — text only</option>
        <option value="auto">auto — send every image attachment</option>
      </select>
      <div className="muted" style={{ fontSize: 11 }}>
        with images set, the call goes to a vision model (free OpenRouter →
        paid Anthropic). OCR text is already in <code>{"{attachment_text}"}</code>{" "}
        for free — only turn this on for visual understanding.
      </div>

      <label className="row" style={{ gap: 6, marginTop: 6 }}>
        <input type="checkbox" style={{ width: "auto" }}
               checked={config.json_schema != null && config.json_schema !== ""}
               onChange={(e) => set({ json_schema: e.target.checked ? { type: "object", properties: {} } : null })} />
        parse the reply as JSON (edit the schema in the raw config below)
      </label>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 90 }}>on error</span>
        <select value={str("on_error", "passthrough")}
                onChange={(e) => set({ on_error: e.target.value })}>
          <option value="passthrough">passthrough (output = null)</option>
          <option value="fail">fail the run</option>
        </select>
      </div>
    </div>
  );
}

function SfContextForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const want = Array.isArray(config.want)
    ? (config.want as string[])
    : ["account", "contacts", "leads", "cases", "team"];
  const toggle = (k: string) =>
    onConfig({
      ...config,
      want: want.includes(k) ? want.filter((x) => x !== k) : [...want, k],
    });
  const OPTS: [string, string][] = [
    ["account", "Account + parent hierarchy (organization)"],
    ["contacts", "Contact + siblings on the Account"],
    ["leads", "Lead (when the sender isn't a Contact)"],
    ["cases", "Related Cases (open / total + recent)"],
    ["team", "Account team / owner (Users)"],
  ];
  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        Loads the Salesforce picture around the Case into{" "}
        <code>state.sf_context</code>. Put it right after <code>identify</code>.
      </div>
      {OPTS.map(([k, label]) => (
        <label className="row" key={k} style={{ gap: 6, marginTop: 4 }}>
          <input type="checkbox" style={{ width: "auto" }}
                 checked={want.includes(k)} onChange={() => toggle(k)} />
          {label}
        </label>
      ))}
    </div>
  );
}

function AttachmentsForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        Fetches image (and, opt-in, video) attachments on the Case → local OCR
        / transcription → folded into <code>classify</code> / <code>draft</code>{" "}
        automatically, and available to <code>ai_prompt</code>’s vision mode.
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 90 }}>source</span>
        <select value={typeof config.source === "string" ? config.source : "salesforce"}
                onChange={(e) => set({ source: e.target.value })}>
          <option value="salesforce">Salesforce (ContentDocument)</option>
          <option value="email">inbound email</option>
          <option value="auto">both</option>
        </select>
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 90 }}>max images</span>
        <input type="number" min={1} max={10}
               value={typeof config.max_images === "number" ? config.max_images : 5}
               onChange={(e) => set({ max_images: Math.max(1, Math.min(10, Number(e.target.value) || 5)) })} />
      </div>
      <label className="row" style={{ gap: 6, marginTop: 4 }}>
        <input type="checkbox" style={{ width: "auto" }}
               checked={config.ocr !== false} onChange={(e) => set({ ocr: e.target.checked })} />
        run OCR on images
      </label>
      <label className="row" style={{ gap: 6, marginTop: 4 }}>
        <input type="checkbox" style={{ width: "auto" }}
               checked={config.skip_signatures !== false}
               onChange={(e) => set({ skip_signatures: e.target.checked })} />
        skip signature / logo images (tiny, banner-shaped, <code>image00x.png</code>,
        or seen before from that sender)
      </label>
      <label className="row" style={{ gap: 6, marginTop: 4 }}>
        <input type="checkbox" style={{ width: "auto" }}
               checked={config.video === true} onChange={(e) => set({ video: e.target.checked })} />
        process video (transcribe audio + OCR keyframes)
      </label>
      {config.video === true && (
        <div className="row" style={{ marginTop: 4 }}>
          <span className="muted" style={{ width: 90 }}>keyframes</span>
          <input type="number" min={1} max={12}
                 value={typeof config.video_frames === "number" ? config.video_frames : 4}
                 onChange={(e) => set({ video_frames: Math.max(1, Math.min(12, Number(e.target.value) || 4)) })} />
          <span className="muted" style={{ width: 70, marginLeft: 8 }}>max secs</span>
          <input type="number" min={30} max={1800}
                 value={typeof config.video_max_seconds === "number" ? config.video_max_seconds : 300}
                 onChange={(e) => set({ video_max_seconds: Math.max(30, Math.min(1800, Number(e.target.value) || 300)) })} />
        </div>
      )}
      {config.video === true && (
        <div className="muted" style={{ fontSize: 11 }}>
          adds the image-heavy <code>faster-whisper</code> + <code>ffmpeg</code>{" "}
          path; leave off unless screen recordings are common. Only the first{" "}
          <em>max secs</em> is processed.
        </div>
      )}
    </div>
  );
}

function NotifyHumanForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  const mention = (config.mention as Record<string, unknown>) || {};
  const channel = typeof config.channel === "string" ? config.channel : "both";
  const rounds = typeof config.max_rounds === "number" ? config.max_rounds : 3;

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        Tags the responsible agent and <strong>opens the Slack reasoning
        dialogue</strong>: the bot picks the questions that matter for this case,
        asks them in one message with its own read, and drafts the customer
        reply only after the agent works through the critical points and
        approves. Nothing is sent automatically. Put this after every terminal
        branch.
      </div>

      <label style={{ marginTop: 6, display: "block" }}>channel</label>
      <select value={channel} onChange={(e) => set({ channel: e.target.value })}>
        <option value="both">Slack + Salesforce Chatter</option>
        <option value="slack">Slack only</option>
        <option value="salesforce_chatter">Salesforce Chatter only</option>
      </select>

      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 130 }}>slack channel</span>
        <input
          value={typeof config.slack_channel === "string" ? config.slack_channel : ""}
          placeholder="#support-escalations or Cxxxxxxxx"
          onChange={(e) => set({ slack_channel: e.target.value.trim() })}
        />
      </div>

      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 130 }}>max clarify rounds</span>
        <input
          type="number"
          min={0}
          max={6}
          value={rounds}
          onChange={(e) => set({ max_rounds: Math.max(0, Math.min(6, Number(e.target.value) || 0)) })}
        />
      </div>
      <div className="muted" style={{ fontSize: 11 }}>
        short follow-ups the bot may send when a <em>critical</em> point is still
        open, before it drafts anyway (default 3).
      </div>

      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 130 }}>@mention (Slack id)</span>
        <input
          value={typeof mention.slack_user_id === "string" ? mention.slack_user_id : ""}
          placeholder="Uxxxxxxxx (else resolved by agent email)"
          onChange={(e) => set({ mention: { ...mention, slack_user_id: e.target.value.trim() } })}
        />
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 130 }}>@mention (SF id)</span>
        <input
          value={typeof mention.mention_id === "string" ? mention.mention_id : ""}
          placeholder="005xxxxxxxxxxxx (Chatter @mention)"
          onChange={(e) => set({ mention: { ...mention, mention_id: e.target.value.trim() } })}
        />
      </div>
    </div>
  );
}

function IdentifyForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  const domainMatch = config.domain_match !== false;
  const freeList = Array.isArray(config.free_email_domains)
    ? (config.free_email_domains as string[]).join("\n")
    : "";

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        resolves the sender against Salesforce → <code>state.sender</code>{" "}
        (exact contact / email-domain → account / unknown). Pass-through.
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 110 }}>email field</span>
        <input
          value={typeof config.email_field === "string" ? config.email_field : "contact.email"}
          placeholder="contact.email"
          onChange={(e) => set({ email_field: e.target.value })}
        />
      </div>
      <label className="row" style={{ gap: 6, marginTop: 4 }}>
        <input
          type="checkbox"
          style={{ width: "auto" }}
          checked={domainMatch}
          onChange={(e) => set({ domain_match: e.target.checked })}
        />
        match email domain → account
      </label>
      <label className="row" style={{ gap: 6 }}>
        <input
          type="checkbox"
          style={{ width: "auto" }}
          checked={config.create_lead_if_missing === true}
          onChange={(e) => set({ create_lead_if_missing: e.target.checked })}
        />
        create a Lead when nothing matched
      </label>
      <div className="field" style={{ marginTop: 6 }}>
        <label>free-mail domains to skip (one per line — blank = built-in list)</label>
        <textarea
          rows={3}
          value={freeList}
          placeholder={"gmail.com\nyahoo.com"}
          onChange={(e) => {
            const arr = e.target.value.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
            set({ free_email_domains: arr.length ? arr : undefined });
          }}
        />
      </div>
    </div>
  );
}

function KbLookupForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const [cols, setCols] = useState<KbCollection[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const selected = (config.collections as string[]) || [];
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });

  useEffect(() => {
    api.kb
      .listCollections()
      .then(setCols)
      .catch((e) => setErr(String(e)));
  }, []);

  const toggle = (name: string) =>
    set({
      collections: selected.includes(name)
        ? selected.filter((n) => n !== name)
        : [...selected, name],
    });

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <label>collections to consult</label>
      {err && <div className="err" style={{ fontSize: 11 }}>{err}</div>}
      {cols.length === 0 && !err && (
        <div className="muted" style={{ fontSize: 11 }}>
          no collections yet — add some in the Knowledge tab
        </div>
      )}
      {cols.map((c) => (
        <label key={c.source_id} className="row" style={{ gap: 6 }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={selected.includes(c.name)}
            onChange={() => toggle(c.name)}
          />
          {c.name} <span className="muted">({c.entry_count})</span>
        </label>
      ))}
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 90 }}>top_k</span>
        <input
          type="number" min="1" max="10"
          value={typeof config.top_k === "number" ? config.top_k : 4}
          onChange={(e) => set({ top_k: parseInt(e.target.value, 10) })}
        />
      </div>
      <div className="field">
        <label>query (optional — {"{{case.subject}}"} etc.; default = case text)</label>
        <input
          value={typeof config.query === "string" ? config.query : ""}
          placeholder="{{case.subject}} {{case.body}}"
          onChange={(e) => set({ query: e.target.value || undefined })}
        />
      </div>
    </div>
  );
}

export function EdgeInspector({
  edge,
  onCondition,
  onDelete,
}: {
  edge: RFEdge;
  onCondition: (c: Record<string, unknown>) => void;
  onDelete: () => void;
}) {
  const ifExpr = (edge.data?.condition as { if?: string })?.if ?? "";
  const conditional = ifExpr !== "";
  return (
    <div>
      <h4>edge</h4>
      <div className="field">
        <label className="row" style={{ gap: 6 }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={conditional}
            onChange={(e) =>
              onCondition(e.target.checked ? { if: "tier == 'enterprise'" } : {})
            }
          />
          conditional
        </label>
      </div>
      {conditional && (
        <div className="field">
          <label>if (expression)</label>
          <textarea
            rows={2}
            value={ifExpr}
            onChange={(e) => onCondition({ if: e.target.value })}
          />
          <div className="muted" style={{ fontSize: 11 }}>
            names: tier, region, confidence, retrieval_score, draft_confidence,
            confidence_gate.pass, classification.urgency
          </div>
        </div>
      )}
      <button className="err" onClick={onDelete}>
        delete edge
      </button>
    </div>
  );
}

function JsonField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(value, null, 2));
  const [err, setErr] = useState<string | null>(null);

  // reflect external changes (e.g. the gate form) unless the user is mid-edit-error
  useEffect(() => {
    if (!err) setText(JSON.stringify(value, null, 2));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(value)]);

  return (
    <div className="field">
      <label>{label}</label>
      <textarea
        rows={10}
        value={text}
        style={err ? { borderColor: "var(--err)" } : undefined}
        onChange={(e) => {
          setText(e.target.value);
          try {
            onChange(JSON.parse(e.target.value));
            setErr(null);
          } catch (x) {
            setErr((x as Error).message);
          }
        }}
      />
      {err && <div className="err" style={{ fontSize: 11 }}>{err}</div>}
    </div>
  );
}

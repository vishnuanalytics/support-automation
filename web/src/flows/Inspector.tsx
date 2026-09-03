import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import type { Connection, KbCollection, SfMeta, SlackMeta } from "../types";
import type { RFEdge, RFNode } from "./graph";

// Salesforce routing metadata (queues + real Case fields/picklists,
// including custom ones) — fetched per (tenant, org) and cached for the
// editor session; a node's own `config.org` picks which connected org
// (empty -> 'default'), so switching a node's org refetches that org's
// real schema instead of showing another org's fields.
const _EMPTY_META: SfMeta = { available: false, queues: [], case_types: [], modules: [], case_fields: [], users: [] };
const _metaCache = new Map<string, SfMeta>();
const _metaPromise = new Map<string, Promise<SfMeta>>();

function useSfMeta(tenantId: string, orgLabel = "default"): SfMeta {
  const key = `${tenantId}:${orgLabel || "default"}`;
  const [meta, setMeta] = useState<SfMeta>(_metaCache.get(key) ?? _EMPTY_META);
  useEffect(() => {
    if (!tenantId) return;
    if (_metaCache.has(key)) {
      setMeta(_metaCache.get(key)!);
      return;
    }
    const p = _metaPromise.get(key) ||
      api.salesforce.meta(tenantId, orgLabel).catch(() => _EMPTY_META);
    _metaPromise.set(key, p);
    p.then((m) => {
      _metaCache.set(key, m);
      setMeta(m);
    });
  }, [key, tenantId, orgLabel]);
  return meta;
}

/** A Salesforce-Queue picker: a <select> of the org's queues when the API
 *  could reach Salesforce, otherwise a plain text box. Keeps the current
 *  value even if it is not (yet) in the list. */
function QueuePicker({
  value,
  onChange,
  placeholder,
  tenantId,
  orgLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  tenantId: string;
  orgLabel?: string;
}) {
  const meta = useSfMeta(tenantId, orgLabel);
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

/** A real Salesforce User OR Queue/Group id — for an @mention / assignment
 *  target (`notify.target_by_type`/`fallback_target`,
 *  `notify_human.mention.mention_id`). Grouped `<optgroup>`s since either
 *  kind is valid for a Chatter mention. Plain text when the org can't be
 *  reached / has neither. */
function SfMentionPicker({
  value,
  onChange,
  tenantId,
  orgLabel,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  tenantId: string;
  orgLabel?: string;
  placeholder?: string;
}) {
  const meta = useSfMeta(tenantId, orgLabel);
  const users = meta.users || [];
  if (!meta.available || (users.length === 0 && meta.queues.length === 0)) {
    return (
      <input
        value={value}
        placeholder={placeholder || "005xxxxxxxxxxxx or 00Gxxxxxxxxxxxx"}
        onChange={(e) => onChange(e.target.value.trim())}
      />
    );
  }
  const known = value === "" || users.some((u) => u.id === value) || meta.queues.some((q) => q.id === value);
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">— none —</option>
      {!known && <option value={value}>{value} (not in org)</option>}
      {users.length > 0 && (
        <optgroup label="Users">
          {users.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
        </optgroup>
      )}
      {meta.queues.length > 0 && (
        <optgroup label="Queues">
          {meta.queues.map((q) => <option key={q.id} value={q.id}>{q.name}</option>)}
        </optgroup>
      )}
    </select>
  );
}

// The tenant's connected Salesforce orgs — fetched once per tenant, shared
// by every node's org picker in this editor session.
const _orgsCache = new Map<string, string[]>();
const _orgsPromise = new Map<string, Promise<string[]>>();

function useConnectedOrgs(tenantId: string): string[] {
  const [orgs, setOrgs] = useState<string[]>(_orgsCache.get(tenantId) ?? []);
  useEffect(() => {
    if (!tenantId) return;
    if (_orgsCache.has(tenantId)) {
      setOrgs(_orgsCache.get(tenantId)!);
      return;
    }
    const p = _orgsPromise.get(tenantId) ||
      api.salesforceOrgs.list(tenantId).then((rows) => rows.map((r) => r.org_label)).catch(() => []);
    _orgsPromise.set(tenantId, p);
    p.then((labels) => {
      _orgsCache.set(tenantId, labels);
      setOrgs(labels);
    });
  }, [tenantId]);
  return orgs;
}

/** Which connected Salesforce org this node should use (blank = 'default').
 *  A plain text fallback when the tenant has 0-1 orgs connected — no point
 *  showing a picker with nothing to pick. */
function OrgPicker({
  value,
  onChange,
  tenantId,
}: {
  value: string;
  onChange: (v: string) => void;
  tenantId: string;
}) {
  const orgs = useConnectedOrgs(tenantId);
  if (orgs.length <= 1) {
    return (
      <input
        value={value}
        placeholder="default"
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <select value={value || "default"} onChange={(e) => onChange(e.target.value)}>
      {orgs.map((label) => (
        <option key={label} value={label}>{label}</option>
      ))}
    </select>
  );
}

// Slack workspace metadata — fetched once per tenant, shared by every
// channel/@mention picker in this editor session (same cache-per-key
// shape as useSfMeta).
const _EMPTY_SLACK: SlackMeta = { available: false, channels: [], users: [], usergroups: [] };
const _slackCache = new Map<string, SlackMeta>();
const _slackPromise = new Map<string, Promise<SlackMeta>>();

function useSlackMeta(tenantId: string): SlackMeta {
  const [meta, setMeta] = useState<SlackMeta>(_slackCache.get(tenantId) ?? _EMPTY_SLACK);
  useEffect(() => {
    if (!tenantId) return;
    if (_slackCache.has(tenantId)) {
      setMeta(_slackCache.get(tenantId)!);
      return;
    }
    const p = _slackPromise.get(tenantId) ||
      api.slack.meta(tenantId).catch(() => _EMPTY_SLACK);
    _slackPromise.set(tenantId, p);
    p.then((m) => {
      _slackCache.set(tenantId, m);
      setMeta(m);
    });
  }, [tenantId]);
  return meta;
}

/** A real Slack channel (`#name` or `Cxxxxxxxx`), fetched live — or a
 *  plain text box when Slack isn't connected / has 0 channels. */
function ChannelPicker({
  value,
  onChange,
  tenantId,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  tenantId: string;
  placeholder?: string;
}) {
  const meta = useSlackMeta(tenantId);
  if (!meta.available || meta.channels.length === 0) {
    return (
      <input
        value={value}
        placeholder={placeholder || "#support-escalations or Cxxxxxxxx"}
        onChange={(e) => onChange(e.target.value.trim())}
      />
    );
  }
  const known = value === "" || meta.channels.some((c) => `#${c.name}` === value || c.id === value);
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">— none —</option>
      {!known && <option value={value}>{value} (not in workspace)</option>}
      {meta.channels.map((c) => (
        <option key={c.id} value={`#${c.name}`}>
          #{c.name}{c.is_member ? "" : "  (bot not in channel)"}
        </option>
      ))}
    </select>
  );
}

/** A real Slack user (`Uxxxxxxxx`), fetched live — or a plain text box
 *  when Slack isn't connected / has 0 visible human users. */
function SlackUserPicker({
  value,
  onChange,
  tenantId,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  tenantId: string;
  placeholder?: string;
}) {
  const meta = useSlackMeta(tenantId);
  if (!meta.available || meta.users.length === 0) {
    return (
      <input
        value={value}
        placeholder={placeholder || "Uxxxxxxxx"}
        onChange={(e) => onChange(e.target.value.trim())}
      />
    );
  }
  const known = value === "" || meta.users.some((u) => u.id === value);
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">— none (resolved by agent email) —</option>
      {!known && <option value={value}>{value} (not in workspace)</option>}
      {meta.users.map((u) => (
        <option key={u.id} value={u.id}>{u.name}{u.email ? ` (${u.email})` : ""}</option>
      ))}
    </select>
  );
}

// The platform-internal keys `sf_writeback`'s field_map can read from
// (interpreter/registry.py::h_sf_writeback's `ctx` dict) — offered as
// suggestions, not a hard list, since a flow can also address nested state.
const SF_WRITEBACK_SRC_KEYS = [
  "urgency", "tier", "region", "topic", "summary",
  "case_type", "case_topic", "case_module", "case_submodule", "case_region",
];

/** `sf_writeback`'s field_map: platform concept -> a REAL field on this
 *  tenant's connected Salesforce org, fetched live — not a hardcoded
 *  platform field name. Falls back to free text when the org isn't
 *  reachable (dry-run / no creds), same degrade-gracefully rule as
 *  QueuePicker. */
function SfWritebackForm({
  config,
  onConfig,
  tenantId,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
  tenantId: string;
}) {
  const org = typeof config.org === "string" ? config.org : "";
  const meta = useSfMeta(tenantId, org || undefined);
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  const fieldMap = (config.field_map as Record<string, string>) || {};
  const rows = Object.entries(fieldMap);
  const fields = meta.case_fields || [];

  const setRows = (next: Record<string, string>) => set({ field_map: next });
  const updateRow = (i: number, k: string, v: string) => {
    const next: Record<string, string> = {};
    rows.forEach(([kk, vv], j) => (next[j === i ? k : kk] = j === i ? v : vv));
    setRows(next);
  };
  const removeRow = (i: number) => {
    const next: Record<string, string> = {};
    rows.forEach(([kk, vv], j) => { if (j !== i) next[kk] = vv; });
    setRows(next);
  };

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        writes triage output onto the Salesforce Case. Left = a platform
        concept, right = a REAL field on the connected org — fetched live,
        {meta.available ? ` ${fields.length} mappable fields found.` : " connect an org (Connections tab) to see them."}
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 90 }}>org</span>
        <OrgPicker value={org} onChange={(v) => set({ org: v || undefined })} tenantId={tenantId} />
      </div>
      <label style={{ marginTop: 6, display: "block" }}>field_map</label>
      <datalist id="sfwb-src-keys">
        {SF_WRITEBACK_SRC_KEYS.map((k) => <option key={k} value={k} />)}
      </datalist>
      {rows.map(([k, v], i) => (
        <div className="row" key={i} style={{ gap: 4 }}>
          <input
            value={k}
            list="sfwb-src-keys"
            placeholder="case_module"
            style={{ maxWidth: 130 }}
            onChange={(e) => updateRow(i, e.target.value, v)}
          />
          <span className="muted">→</span>
          {fields.length > 0 ? (
            <select value={v} onChange={(e) => updateRow(i, k, e.target.value)} style={{ flex: 1 }}>
              <option value="">— pick a field —</option>
              {!fields.some((f) => f.name === v) && v && <option value={v}>{v} (not in org)</option>}
              {fields.map((f) => (
                <option key={f.name} value={f.name}>
                  {f.label} ({f.name}){f.picklist_values.length ? ` — picklist` : ""}
                </option>
              ))}
            </select>
          ) : (
            <input
              value={v}
              placeholder="Module__c"
              style={{ flex: 1 }}
              onChange={(e) => updateRow(i, k, e.target.value)}
            />
          )}
          <button onClick={() => removeRow(i)} title="remove">✕</button>
        </div>
      ))}
      <button style={{ marginTop: 4 }} onClick={() => setRows({ ...fieldMap, "": "" })}>
        + map a field
      </button>
      <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
        value_maps / append are edited in the raw config below — this form
        covers field_map only for now.
      </div>
    </div>
  );
}

export function NodeInspector({
  node,
  config,
  onLabel,
  onConfig,
  onDelete,
  tenantId,
}: {
  node: RFNode;
  config: Record<string, unknown>;
  onLabel: (v: string) => void;
  onConfig: (v: Record<string, unknown>) => void;
  onDelete: () => void;
  tenantId: string;
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
        <ClarifyForm config={config} onConfig={onConfig} tenantId={tenantId} />
      )}

      {node.data.nodeType === "notify" && (
        <NotifyForm config={config} onConfig={onConfig} tenantId={tenantId} />
      )}

      {node.data.nodeType === "sf_writeback" && (
        <SfWritebackForm config={config} onConfig={onConfig} tenantId={tenantId} />
      )}

      {(node.data.nodeType === "ask_human" || node.data.nodeType === "handover") && (
        <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
          <label>queue</label>
          <QueuePicker
            value={typeof config.queue === "string" ? config.queue : ""}
            onChange={(v) => onConfig({ ...config, queue: v || undefined })}
            tenantId={tenantId}
            orgLabel={typeof config.org === "string" ? config.org : undefined}
          />
          <div className="row" style={{ marginTop: 6 }}>
            <span className="muted" style={{ width: 90 }}>org</span>
            <OrgPicker
              value={typeof config.org === "string" ? config.org : ""}
              onChange={(v) => onConfig({ ...config, org: v || undefined })}
              tenantId={tenantId}
            />
          </div>
          <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
            queue_by_team / enterprise_queue overrides are edited in the raw
            config below.
          </div>
        </div>
      )}

      {node.data.nodeType === "notify_human" && (
        <NotifyHumanForm config={config} onConfig={onConfig} tenantId={tenantId} />
      )}

      {node.data.nodeType === "ai_prompt" && (
        <AiPromptForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "sf_context" && (
        <SfContextForm config={config} onConfig={onConfig} tenantId={tenantId} />
      )}

      {node.data.nodeType === "sf_case" && (
        <SfCaseForm config={config} onConfig={onConfig} tenantId={tenantId} />
      )}

      {node.data.nodeType === "retrieve" && (
        <RetrieveForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "http_request" && (
        <HttpRequestForm config={config} onConfig={onConfig} tenantId={tenantId} />
      )}

      {node.data.nodeType === "attachments" && (
        <AttachmentsForm config={config} onConfig={onConfig} tenantId={tenantId} />
      )}

      {node.data.nodeType === "identify" && (
        <IdentifyForm config={config} onConfig={onConfig} tenantId={tenantId} />
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
  tenantId,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
  tenantId: string;
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
          tenantId={tenantId}
          orgLabel={typeof config.org === "string" ? config.org : undefined}
        />
      </div>
      <div className="muted" style={{ fontSize: 11 }}>
        after <code>max rounds</code> of asking the customer, the Case is
        reassigned to this queue (blank = stay put, note only).
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 110 }}>org</span>
        <OrgPicker
          value={typeof config.org === "string" ? config.org : ""}
          onChange={(v) => set({ org: v || undefined })}
          tenantId={tenantId}
        />
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
  tenantId,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
  tenantId: string;
}) {
  const meta = useSfMeta(tenantId, typeof config.org === "string" ? config.org : undefined);
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
          <SfMentionPicker
            value={byType[t] ?? ""}
            onChange={(v) => setTarget(t, v)}
            tenantId={tenantId}
            orgLabel={typeof config.org === "string" ? config.org : undefined}
            placeholder="User/Group id or name"
          />
        </div>
      ))}
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 110 }}>fallback target</span>
        <SfMentionPicker
          value={typeof config.fallback_target === "string" ? config.fallback_target : ""}
          onChange={(v) => set({ fallback_target: v || null })}
          tenantId={tenantId}
          orgLabel={typeof config.org === "string" ? config.org : undefined}
          placeholder="(optional)"
        />
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 110 }}>org</span>
        <OrgPicker
          value={typeof config.org === "string" ? config.org : ""}
          onChange={(v) => set({ org: v || undefined })}
          tenantId={tenantId}
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
  tenantId,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
  tenantId: string;
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
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 90 }}>org</span>
        <OrgPicker
          value={typeof config.org === "string" ? config.org : ""}
          onChange={(v) => onConfig({ ...config, org: v || undefined })}
          tenantId={tenantId}
        />
      </div>
    </div>
  );
}

/** Origin/Status pickers pull real picklist values from `meta.case_fields`
 *  by field name; a plain text box when the org isn't reachable / the
 *  field isn't found (e.g. a heavily customized Status picklist). */
function PicklistPicker({
  fieldName,
  value,
  onChange,
  meta,
  placeholder,
}: {
  fieldName: string;
  value: string;
  onChange: (v: string) => void;
  meta: SfMeta;
  placeholder?: string;
}) {
  const field = (meta.case_fields || []).find((f) => f.name === fieldName);
  if (!field || field.picklist_values.length === 0) {
    return <input value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />;
  }
  const known = value === "" || field.picklist_values.some((v) => v.value === value);
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">— default —</option>
      {!known && <option value={value}>{value} (not in org)</option>}
      {field.picklist_values.map((v) => (
        <option key={v.value} value={v.value}>{v.label}</option>
      ))}
    </select>
  );
}

function SfCaseForm({
  config,
  onConfig,
  tenantId,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
  tenantId: string;
}) {
  const org = typeof config.org === "string" ? config.org : "";
  const meta = useSfMeta(tenantId, org || undefined);
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        Resolves the inbound message to a real Salesforce Case — creating
        the Contact/Account/Case as needed, or reusing an open Case for a
        thread reply.
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 90 }}>org</span>
        <OrgPicker value={org} onChange={(v) => set({ org: v || undefined })} tenantId={tenantId} />
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 90 }}>origin</span>
        <PicklistPicker fieldName="Origin" value={typeof config.origin === "string" ? config.origin : "Email"}
                        onChange={(v) => set({ origin: v || undefined })} meta={meta} placeholder="Email" />
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 90 }}>status</span>
        <PicklistPicker fieldName="Status" value={typeof config.status === "string" ? config.status : "New"}
                        onChange={(v) => set({ status: v || undefined })} meta={meta} placeholder="New" />
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 90 }}>reuse</span>
        <select value={typeof config.reuse === "string" ? config.reuse : "thread"}
                onChange={(e) => set({ reuse: e.target.value })}>
          <option value="thread">reuse an open Case for a thread reply</option>
          <option value="never">always create a new Case</option>
        </select>
      </div>
      <label className="row" style={{ gap: 6, marginTop: 6 }}>
        <input type="checkbox" style={{ width: "auto" }}
               checked={config.create_contact !== false}
               onChange={(e) => set({ create_contact: e.target.checked })} />
        create the Contact if missing
      </label>
      <label className="row" style={{ gap: 6, marginTop: 4 }}>
        <input type="checkbox" style={{ width: "auto" }}
               checked={config.create_account !== false}
               onChange={(e) => set({ create_account: e.target.checked })} />
        create the Account if missing (business-domain senders)
      </label>
    </div>
  );
}

function RetrieveForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const [cols, setCols] = useState<KbCollection[]>([]);
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  const selected = (config.kb_sources as string[]) || [];

  useEffect(() => {
    api.kb.listCollections().then(setCols).catch(() => setCols([]));
  }, []);

  const toggle = (name: string) =>
    set({
      kb_sources: selected.includes(name)
        ? selected.filter((n) => n !== name)
        : [...selected, name],
    });

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <label>kb_sources (blank = every collection this tenant can reach)</label>
      {cols.length === 0 && (
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
          value={typeof config.top_k === "number" ? config.top_k : 5}
          onChange={(e) => set({ top_k: parseInt(e.target.value, 10) })}
        />
      </div>
    </div>
  );
}

/** `http_request.connection`: a real per-tenant Connection slug (Data tab),
 *  fetched live — the allow-list this node's base URL/auth actually come
 *  from. Falls back to text when the tenant has 0 connections. */
function HttpRequestForm({
  config,
  onConfig,
  tenantId,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
  tenantId: string;
}) {
  const [conns, setConns] = useState<Connection[]>([]);
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });

  useEffect(() => {
    if (!tenantId) return;
    api.connections.list(tenantId).then(setConns).catch(() => setConns([]));
  }, [tenantId]);

  const value = typeof config.connection === "string" ? config.connection : "";
  const method = typeof config.method === "string" ? config.method : "GET";

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <label>connection</label>
      {conns.length === 0 ? (
        <input value={value} placeholder="connection slug (Data tab)"
               onChange={(e) => set({ connection: e.target.value.trim() })} />
      ) : (
        <select value={value} onChange={(e) => set({ connection: e.target.value })}>
          <option value="">— pick a connection —</option>
          {!conns.some((c) => c.slug === value) && value && <option value={value}>{value} (not found)</option>}
          {conns.map((c) => (
            <option key={c.slug} value={c.slug}>{c.slug} — {c.base_url}</option>
          ))}
        </select>
      )}
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 90 }}>method</span>
        <select value={method} onChange={(e) => set({ method: e.target.value })}>
          {["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
      </div>
      <div className="field">
        <label>path ({"{{ dotted.path }}"} templated)</label>
        <input value={typeof config.path === "string" ? config.path : ""}
               placeholder="/v1/things/{{context.id}}"
               onChange={(e) => set({ path: e.target.value })} />
      </div>
      <div className="muted" style={{ fontSize: 11 }}>
        query / headers / body / out_key are edited in the raw config below.
      </div>
    </div>
  );
}

function AttachmentsForm({
  config,
  onConfig,
  tenantId,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
  tenantId: string;
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
      {config.source !== "email" && (
        <div className="row" style={{ marginTop: 6 }}>
          <span className="muted" style={{ width: 90 }}>org</span>
          <OrgPicker
            value={typeof config.org === "string" ? config.org : ""}
            onChange={(v) => set({ org: v || undefined })}
            tenantId={tenantId}
          />
        </div>
      )}
    </div>
  );
}

function NotifyHumanForm({
  config,
  onConfig,
  tenantId,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
  tenantId: string;
}) {
  const meta = useSlackMeta(tenantId);
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
        <span className="muted" style={{ width: 130 }}>
          slack channel {meta.available && <span className="muted">(live)</span>}
        </span>
        <ChannelPicker
          value={typeof config.slack_channel === "string" ? config.slack_channel : ""}
          onChange={(v) => set({ slack_channel: v })}
          tenantId={tenantId}
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
        <SlackUserPicker
          value={typeof mention.slack_user_id === "string" ? mention.slack_user_id : ""}
          onChange={(v) => set({ mention: { ...mention, slack_user_id: v } })}
          tenantId={tenantId}
        />
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 130 }}>@mention (SF id)</span>
        <SfMentionPicker
          value={typeof mention.mention_id === "string" ? mention.mention_id : ""}
          onChange={(v) => set({ mention: { ...mention, mention_id: v } })}
          tenantId={tenantId}
          orgLabel={typeof config.org === "string" ? config.org : undefined}
          placeholder="005xxxxxxxxxxxx (Chatter @mention)"
        />
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <span className="muted" style={{ width: 130 }}>SF org (for the mention lookup)</span>
        <OrgPicker
          value={typeof config.org === "string" ? config.org : ""}
          onChange={(v) => set({ org: v || undefined })}
          tenantId={tenantId}
        />
      </div>

      <ByTeamOverride
        label="channel override by routed_team"
        value={(config.slack_channel_by_team as Record<string, string>) || {}}
        onChange={(v) => set({ slack_channel_by_team: v })}
        renderPicker={(v, onV) => <ChannelPicker value={v} onChange={onV} tenantId={tenantId} />}
      />
      <ByTeamOverride
        label="@mention override by routed_team"
        value={(mention.slack_user_by_team as Record<string, string>) || {}}
        onChange={(v) => set({ mention: { ...mention, slack_user_by_team: v } })}
        renderPicker={(v, onV) => <SlackUserPicker value={v} onChange={onV} tenantId={tenantId} />}
      />
    </div>
  );
}

const ROUTED_TEAMS = ["support", "csm", "sales", "offboarding"];

/** A team -> (something with a real picker) map, e.g. `slack_channel_by_team`.
 *  Rows for the known `team_route` teams plus any custom key already in the
 *  config; a select at the bottom adds a team not shown yet. */
function ByTeamOverride({
  label,
  value,
  onChange,
  renderPicker,
}: {
  label: string;
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
  renderPicker: (v: string, onChange: (v: string) => void) => ReactNode;
}) {
  const shown = Object.keys(value).filter((k) => k !== "default");
  const addable = ROUTED_TEAMS.filter((t) => !shown.includes(t));
  return (
    <div style={{ marginTop: 8 }}>
      <label style={{ display: "block" }}>{label}</label>
      {shown.map((team) => (
        <div className="row" key={team} style={{ gap: 4 }}>
          <span className="muted" style={{ width: 90 }}>{team}</span>
          {renderPicker(value[team] ?? "", (v) => {
            const next = { ...value };
            if (v) next[team] = v; else delete next[team];
            onChange(next);
          })}
          <button onClick={() => { const next = { ...value }; delete next[team]; onChange(next); }}>✕</button>
        </div>
      ))}
      {addable.length > 0 && (
        <select value="" onChange={(e) => {
          if (e.target.value) onChange({ ...value, [e.target.value]: "" });
        }}>
          <option value="">+ add a team override…</option>
          {addable.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      )}
    </div>
  );
}

function IdentifyForm({
  config,
  onConfig,
  tenantId,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
  tenantId: string;
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
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 90 }}>org</span>
        <OrgPicker
          value={typeof config.org === "string" ? config.org : ""}
          onChange={(v) => set({ org: v || undefined })}
          tenantId={tenantId}
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
  tenantId,
}: {
  edge: RFEdge;
  onCondition: (c: Record<string, unknown>) => void;
  onDelete: () => void;
  tenantId: string;
}) {
  const ifExpr = (edge.data?.condition as { if?: string })?.if ?? "";
  const conditional = ifExpr !== "";
  // Default org's schema -- an edge isn't scoped to one node's `config.org`,
  // so this offers real values from whichever org this tenant treats as
  // primary. Good enough for "insert a real picklist value"; a multi-org
  // tenant branching on a non-default org's Type values still has the
  // plain expression box. (No Slack-backed condition exists -- edge
  // conditions only ever see `_context()`'s state fields, which don't
  // include anything from Slack, so there's nothing genuine to pick from
  // there.)
  const sfMeta = useSfMeta(tenantId);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const insert = (snippet: string) => {
    const ta = taRef.current;
    const start = ta?.selectionStart ?? ifExpr.length;
    const end = ta?.selectionEnd ?? ifExpr.length;
    const next = ifExpr.slice(0, start) + snippet + ifExpr.slice(end);
    onCondition({ if: next });
    if (ta) {
      const pos = start + snippet.length;
      requestAnimationFrame(() => { ta.focus(); ta.setSelectionRange(pos, pos); });
    }
  };

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
            ref={taRef}
            rows={2}
            value={ifExpr}
            onChange={(e) => onCondition({ if: e.target.value })}
          />
          <div className="muted" style={{ fontSize: 11 }}>
            names: tier, region, confidence, retrieval_score, draft_confidence,
            confidence_gate.pass, classification.urgency, classification.case_type,
            routed_team, sf_context.*
          </div>
          {sfMeta.case_types.length > 0 && (
            <div className="row" style={{ marginTop: 6, gap: 4, flexWrap: "wrap" }}>
              {sfMeta.case_types.length > 0 && (
                <select value="" onChange={(e) => {
                  if (e.target.value) insert(`classification.case_type == '${e.target.value}'`);
                }}>
                  <option value="">+ Case Type…</option>
                  {sfMeta.case_types.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              )}
              <select value="" onChange={(e) => {
                if (e.target.value) insert(`routed_team == '${e.target.value}'`);
              }}>
                <option value="">+ routed_team…</option>
                {ROUTED_TEAMS.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
              <button type="button" onClick={() => insert(" && ")}>&&</button>
              <button type="button" onClick={() => insert(" || ")}>||</button>
            </div>
          )}
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

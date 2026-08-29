import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { ActionRequest, FlowMeta, PolicyRule } from "../types";

/**
 * Phase 16 — structured policy rules + the approval queue.
 *
 * A rule is {when: <predicate>, then: <route|task>}. `when` / `then` are
 * edited as JSON here (the Inspector uses the same raw-JSON pattern); a
 * form builder is a follow-on. `then.type='task'` posts a Slack
 * Approve/Reject; approved tasks open a GitHub issue.
 */
export function RulesView() {
  const [teams, setTeams] = useState<string[]>([]);
  const [team, setTeam] = useState<string>("");
  const [rules, setRules] = useState<PolicyRule[]>([]);
  const [reqs, setReqs] = useState<ActionRequest[]>([]);
  const [slack, setSlack] = useState<{ configured: boolean; connected: Record<string, boolean> }>({
    configured: false,
    connected: {},
  });
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.listFlows().then((fs: FlowMeta[]) => {
      const ts = [...new Set(fs.map((f) => f.team))].sort();
      setTeams(ts);
      setTeam((t) => t || ts[0] || "");
    });
    api.slack.status().then(setSlack).catch(() => {});
  }, []);

  const refresh = useCallback(async () => {
    if (!team) return;
    try {
      setRules(await api.rules.list(team));
      setReqs(await api.actionRequests(30));
      setErr(null);
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }, [team]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const tenantId = rules[0]?.tenant_id;
  const slackConnected = tenantId ? slack.connected[tenantId] : false;

  async function connectSlack() {
    if (!tenantId) return;
    const { url } = await api.slack.authorize(tenantId);
    const w = window.open(url, "slack-oauth", "width=520,height=720");
    const t = setInterval(() => {
      if (w?.closed) {
        clearInterval(t);
        api.slack.status().then(setSlack).catch(() => {});
      }
    }, 800);
  }

  async function addRule() {
    const name = prompt("rule name")?.trim();
    if (!name || !team) return;
    try {
      await api.rules.create({
        team,
        name,
        priority: 100,
        when: { field: "tier", op: "eq", value: "premium" },
        then: { type: "route", action: "ask_human" },
      });
      void refresh();
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  return (
    <div className="col" style={{ padding: 16, gap: 16, overflow: "auto" }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <div className="row" style={{ gap: 8 }}>
          <strong>Policy rules</strong>
          <select value={team} onChange={(e) => setTeam(e.target.value)} style={{ width: "auto" }}>
            {teams.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
          <button onClick={addRule}>＋ rule</button>
        </div>
        <div className="row">
          {slack.configured &&
            (slackConnected ? (
              <span className="muted" style={{ fontSize: 12 }}>Slack ✓</span>
            ) : (
              <button onClick={connectSlack} disabled={!tenantId}>Connect Slack</button>
            ))}
        </div>
      </div>
      {err && <div className="err" style={{ fontSize: 12 }}>{err}</div>}

      <div className="col" style={{ gap: 10 }}>
        {rules.map((r) => (
          <RuleEditor key={r.rule_id} rule={r} onChange={refresh} />
        ))}
        {rules.length === 0 && (
          <div className="muted" style={{ fontSize: 12 }}>
            no rules for “{team}”. A <code>policy_gate</code> node in that team’s flow
            evaluates these; a <code>task_dispatch</code> node acts on a
            <code> then.type=&quot;task&quot;</code> match.
          </div>
        )}
      </div>

      <div>
        <strong>Approval queue</strong>
        <table className="runs-table" style={{ marginTop: 6 }}>
          <thead>
            <tr>
              <th>title</th>
              <th>rule</th>
              <th>status</th>
              <th>decided</th>
            </tr>
          </thead>
          <tbody>
            {reqs.map((a) => (
              <tr key={a.id}>
                <td>
                  {String((a.payload as { title?: string }).title ?? a.kind)}
                  {a.result && (a.result as { html_url?: string }).html_url && (
                    <>
                      {" "}
                      <a href={String((a.result as { html_url: string }).html_url)} target="_blank" rel="noreferrer">
                        issue ↗
                      </a>
                    </>
                  )}
                  {a.error && <span className="err" style={{ fontSize: 11 }}> · {a.error}</span>}
                </td>
                <td className="muted">{a.rule_name ?? "—"}</td>
                <td>{a.status}</td>
                <td className="muted">
                  {a.decided_by ? `${a.decided_by}` : "—"}
                  {a.decided_at ? ` · ${new Date(a.decided_at).toLocaleString()}` : ""}
                </td>
              </tr>
            ))}
            {reqs.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">nothing dispatched yet</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}


// ── rule editor: form + JSON fallback ──────────────────────────────────
type Cond = { field: string; op: string; value: unknown };
type Group = { all?: Node[]; any?: Node[] };
type Node = Cond | Group | { not: Node };

const OPS = ["eq", "ne", "in", "nin", "gt", "gte", "lt", "lte", "contains", "icontains", "exists"];
const FIELD_HINTS = [
  "tier", "region", "classification.topic", "classification.urgency",
  "entities.report_age_years", "retrieval_score", "groundedness.score",
];

const isGroup = (n: Node): n is Group => "all" in n || "any" in n;
const isNot = (n: Node): n is { not: Node } => "not" in n;

function emptyCond(): Cond {
  return { field: "tier", op: "eq", value: "premium" };
}

function RuleEditor({ rule, onChange }: { rule: PolicyRule; onChange: () => void }) {
  const [name, setName] = useState(rule.name);
  const [priority, setPriority] = useState(rule.priority);
  const [status, setStatus] = useState(rule.status);
  const [when, setWhen] = useState<Node>(() => (rule.when as Node) ?? emptyCond());
  const [then, setThen] = useState<Record<string, unknown>>(() => rule.then ?? { type: "route", action: "ask_human" });
  const [raw, setRaw] = useState(false);
  const [rawWhen, setRawWhen] = useState(() => JSON.stringify(rule.when, null, 2));
  const [rawThen, setRawThen] = useState(() => JSON.stringify(rule.then, null, 2));
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const rawParsed = useMemo(() => {
    if (!raw) return { ok: true as const };
    try {
      return { ok: true as const, when: JSON.parse(rawWhen), then: JSON.parse(rawThen) };
    } catch (e) {
      return { ok: false as const, msg: (e as Error).message };
    }
  }, [raw, rawWhen, rawThen]);

  async function save() {
    const payloadWhen = raw ? (rawParsed.ok ? rawParsed.when : null) : when;
    const payloadThen = raw ? (rawParsed.ok ? rawParsed.then : null) : then;
    if (payloadWhen == null || payloadThen == null) return;
    setBusy(true);
    setErr(null);
    try {
      await api.rules.update(rule.rule_id, {
        name,
        priority,
        status,
        when: payloadWhen as Record<string, unknown>,
        then: payloadThen as Record<string, unknown>,
      });
      onChange();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!confirm(`delete rule "${rule.name}"?`)) return;
    await api.rules.remove(rule.rule_id);
    onChange();
  }

  function toggleRaw() {
    if (!raw) {
      setRawWhen(JSON.stringify(when, null, 2));
      setRawThen(JSON.stringify(then, null, 2));
    } else if (rawParsed.ok) {
      setWhen(rawParsed.when as Node);
      setThen(rawParsed.then as Record<string, unknown>);
    }
    setRaw(!raw);
  }

  return (
    <div className="col" style={{ gap: 8, border: "1px solid var(--border)", borderRadius: 6, padding: 10 }}>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <input value={name} onChange={(e) => setName(e.target.value)} style={{ maxWidth: 220 }} />
        <span className="muted" style={{ fontSize: 12 }}>priority</span>
        <input type="number" value={priority} onChange={(e) => setPriority(parseInt(e.target.value, 10))} style={{ width: 70 }} />
        <select value={status} onChange={(e) => setStatus(e.target.value as PolicyRule["status"])} style={{ width: "auto" }}>
          <option value="active">active</option>
          <option value="disabled">disabled</option>
        </select>
        <div style={{ flex: 1 }} />
        <button onClick={toggleRaw} title="edit the raw JSON">{raw ? "form" : "JSON"}</button>
        <button className="primary" onClick={save} disabled={busy || (raw && !rawParsed.ok)}>save</button>
        <button className="err" onClick={remove}>delete</button>
      </div>

      {raw ? (
        <div className="row" style={{ gap: 8, alignItems: "flex-start" }}>
          <div className="field" style={{ flex: 1 }}>
            <label>when</label>
            <textarea rows={8} value={rawWhen} onChange={(e) => setRawWhen(e.target.value)}
              style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }} />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>then</label>
            <textarea rows={8} value={rawThen} onChange={(e) => setRawThen(e.target.value)}
              style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }} />
          </div>
        </div>
      ) : (
        <>
          <div className="field">
            <label>when</label>
            <NodeEditor node={when} onChange={setWhen} onDelete={() => setWhen(emptyCond())} depth={0} />
          </div>
          <ThenEditor then={then} onChange={setThen} />
        </>
      )}
      {raw && !rawParsed.ok && <div className="err" style={{ fontSize: 11 }}>{rawParsed.msg}</div>}
      {err && <div className="err" style={{ fontSize: 11 }}>{err}</div>}
    </div>
  );
}

function NodeEditor({
  node,
  onChange,
  onDelete,
  depth,
}: {
  node: Node;
  onChange: (n: Node) => void;
  onDelete: () => void;
  depth: number;
}) {
  if (isNot(node)) {
    return (
      <div style={{ borderLeft: "2px solid var(--err)", paddingLeft: 8 }}>
        <div className="row" style={{ gap: 6, marginBottom: 4 }}>
          <strong className="muted" style={{ fontSize: 12 }}>NOT</strong>
          <button onClick={() => onChange(node.not)}>unwrap</button>
          <button className="err" onClick={onDelete}>×</button>
        </div>
        <NodeEditor node={node.not} onChange={(n) => onChange({ not: n })} onDelete={onDelete} depth={depth + 1} />
      </div>
    );
  }

  if (isGroup(node)) {
    const kind: "all" | "any" = "all" in node ? "all" : "any";
    const kids = (node[kind] ?? []) as Node[];
    const setKids = (ks: Node[]) => onChange({ [kind]: ks } as Group);
    return (
      <div style={{ borderLeft: "2px solid var(--border)", paddingLeft: 8 }}>
        <div className="row" style={{ gap: 6, marginBottom: 4 }}>
          <select
            value={kind}
            style={{ width: "auto" }}
            onChange={(e) => onChange({ [e.target.value]: kids } as Group)}
          >
            <option value="all">ALL of</option>
            <option value="any">ANY of</option>
          </select>
          <button onClick={() => setKids([...kids, emptyCond()])}>＋ condition</button>
          <button onClick={() => setKids([...kids, { all: [emptyCond()] }])}>＋ group</button>
          {depth > 0 && <button className="err" onClick={onDelete}>×</button>}
        </div>
        <div className="col" style={{ gap: 6 }}>
          {kids.map((k, i) => (
            <NodeEditor
              key={i}
              node={k}
              depth={depth + 1}
              onChange={(n) => setKids(kids.map((x, j) => (j === i ? n : x)))}
              onDelete={() => setKids(kids.filter((_, j) => j !== i))}
            />
          ))}
          {kids.length === 0 && <span className="muted" style={{ fontSize: 11 }}>empty group never matches</span>}
        </div>
      </div>
    );
  }

  // leaf condition
  const c = node as Cond;
  const set = (patch: Partial<Cond>) => onChange({ ...c, ...patch });
  return (
    <div className="row" style={{ gap: 4, alignItems: "center", flexWrap: "wrap" }}>
      <input
        list="rule-fields"
        value={c.field}
        placeholder="entities.x"
        style={{ maxWidth: 190 }}
        onChange={(e) => set({ field: e.target.value })}
      />
      <select value={c.op} style={{ width: "auto" }} onChange={(e) => set({ op: e.target.value })}>
        {OPS.map((o) => (
          <option key={o}>{o}</option>
        ))}
      </select>
      {c.op === "exists" ? (
        <select
          value={String(c.value !== false)}
          style={{ width: "auto" }}
          onChange={(e) => set({ value: e.target.value === "true" })}
        >
          <option value="true">present</option>
          <option value="false">absent</option>
        </select>
      ) : c.op === "in" || c.op === "nin" ? (
        <input
          value={Array.isArray(c.value) ? (c.value as unknown[]).join(", ") : ""}
          placeholder="a, b, c"
          onChange={(e) => set({ value: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })}
        />
      ) : (
        <input
          value={c.value == null ? "" : String(c.value)}
          onChange={(e) => {
            const v = e.target.value;
            const n = Number(v);
            set({ value: v !== "" && !Number.isNaN(n) ? n : v });
          }}
        />
      )}
      <button onClick={() => onChange({ all: [c] })} title="wrap in a group">group</button>
      <button onClick={() => onChange({ not: c })} title="negate">not</button>
      <button className="err" onClick={onDelete}>×</button>
      <datalist id="rule-fields">
        {FIELD_HINTS.map((f) => (
          <option key={f} value={f} />
        ))}
      </datalist>
    </div>
  );
}

function ThenEditor({
  then,
  onChange,
}: {
  then: Record<string, unknown>;
  onChange: (t: Record<string, unknown>) => void;
}) {
  const type = (then.type as string) || "route";
  const set = (patch: Record<string, unknown>) => onChange({ ...then, ...patch });
  const approval = (then.approval as Record<string, string>) || {};

  return (
    <div className="field" style={{ borderTop: "1px solid var(--border)", paddingTop: 8 }}>
      <label>then</label>
      <div className="row" style={{ gap: 8 }}>
        <label className="row" style={{ gap: 4 }}>
          <input type="radio" style={{ width: "auto" }} checked={type === "route"}
            onChange={() => onChange({ type: "route", action: "ask_human" })} />
          route
        </label>
        <label className="row" style={{ gap: 4 }}>
          <input type="radio" style={{ width: "auto" }} checked={type === "task"}
            onChange={() => onChange({ type: "task", task: "github_issue", repo: "", title_tmpl: "Support action: {{case.subject}}", body_tmpl: "{{case.body}}", approval: { slack_channel: "#support-leads" } })} />
          task (Slack-approved GitHub issue)
        </label>
      </div>

      {type === "route" ? (
        <div className="row" style={{ marginTop: 6 }}>
          <span className="muted" style={{ width: 90 }}>action</span>
          <select value={(then.action as string) || "ask_human"} style={{ width: "auto" }}
            onChange={(e) => set({ action: e.target.value })}>
            <option value="ask_human">ask_human</option>
            <option value="auto_approve">auto_approve</option>
            <option value="handover">handover</option>
          </select>
        </div>
      ) : (
        <div className="col" style={{ gap: 6, marginTop: 6 }}>
          <div className="row"><span className="muted" style={{ width: 90 }}>repo</span>
            <input value={(then.repo as string) || ""} placeholder="owner/name" onChange={(e) => set({ repo: e.target.value })} /></div>
          <div className="row"><span className="muted" style={{ width: 90 }}>title</span>
            <input value={(then.title_tmpl as string) || ""} onChange={(e) => set({ title_tmpl: e.target.value })} /></div>
          <div className="field"><label>body template</label>
            <textarea rows={3} value={(then.body_tmpl as string) || ""} onChange={(e) => set({ body_tmpl: e.target.value })} /></div>
          <div className="row"><span className="muted" style={{ width: 90 }}>labels</span>
            <input value={Array.isArray(then.labels) ? (then.labels as string[]).join(", ") : ""}
              placeholder="a, b" onChange={(e) => set({ labels: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })} /></div>
          <div className="row"><span className="muted" style={{ width: 90 }}>approve in</span>
            <input value={approval.slack_channel || approval.slack_user || ""} placeholder="#support-leads or U0123"
              onChange={(e) => {
                const v = e.target.value.trim();
                set({ approval: v.startsWith("#") || v === "" ? { slack_channel: v } : { slack_user: v } });
              }} /></div>
        </div>
      )}
      <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        templates use {"{{case.subject}}"}, {"{{case.body}}"}, {"{{entities.x}}"}
      </div>
    </div>
  );
}

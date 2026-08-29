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

function RuleEditor({ rule, onChange }: { rule: PolicyRule; onChange: () => void }) {
  const [name, setName] = useState(rule.name);
  const [priority, setPriority] = useState(rule.priority);
  const [status, setStatus] = useState(rule.status);
  const [whenText, setWhenText] = useState(() => JSON.stringify(rule.when, null, 2));
  const [thenText, setThenText] = useState(() => JSON.stringify(rule.then, null, 2));
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const parsed = useMemo(() => {
    try {
      return { when: JSON.parse(whenText), then: JSON.parse(thenText), ok: true as const };
    } catch (e) {
      return { ok: false as const, msg: (e as Error).message };
    }
  }, [whenText, thenText]);

  async function save() {
    if (!parsed.ok) return;
    setBusy(true);
    setErr(null);
    try {
      await api.rules.update(rule.rule_id, {
        name,
        priority,
        status,
        when: parsed.when,
        then: parsed.then,
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

  return (
    <div className="col" style={{ gap: 6, border: "1px solid var(--border)", borderRadius: 6, padding: 10 }}>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <input value={name} onChange={(e) => setName(e.target.value)} style={{ maxWidth: 220 }} />
        <span className="muted" style={{ fontSize: 12 }}>priority</span>
        <input
          type="number"
          value={priority}
          onChange={(e) => setPriority(parseInt(e.target.value, 10))}
          style={{ width: 70 }}
        />
        <select value={status} onChange={(e) => setStatus(e.target.value as PolicyRule["status"])} style={{ width: "auto" }}>
          <option value="active">active</option>
          <option value="disabled">disabled</option>
        </select>
        <div style={{ flex: 1 }} />
        <button className="primary" onClick={save} disabled={busy || !parsed.ok}>save</button>
        <button className="err" onClick={remove}>delete</button>
      </div>
      <div className="row" style={{ gap: 8, alignItems: "flex-start" }}>
        <div className="field" style={{ flex: 1 }}>
          <label>when (predicate)</label>
          <textarea rows={7} value={whenText} onChange={(e) => setWhenText(e.target.value)}
            style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }} />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label>then (route | task)</label>
          <textarea rows={7} value={thenText} onChange={(e) => setThenText(e.target.value)}
            style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }} />
        </div>
      </div>
      <div className="muted" style={{ fontSize: 11 }}>
        ops: eq ne in nin gt gte lt lte contains icontains exists ·{" "}
        {`{"all":[…]} / {"any":[…]} / {"not":…} / {"field":"entities.x","op":"gte","value":2}`} ·{" "}
        then: {`{"type":"route","action":"ask_human"}`} or{" "}
        {`{"type":"task","task":"github_issue","repo":"owner/name","title_tmpl":"…","approval":{"slack_channel":"#ops"}}`}
      </div>
      {!parsed.ok && <div className="err" style={{ fontSize: 11 }}>{parsed.msg}</div>}
      {err && <div className="err" style={{ fontSize: 11 }}>{err}</div>}
    </div>
  );
}

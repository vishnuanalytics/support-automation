import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { FlowCandidate, FlowMeta, TemplateMeta } from "../types";
import { Button, Dialog, Field, Input, Textarea, Banner, useToast } from "../ui";

type Handoff = { candidate?: FlowCandidate; mermaidPrompt?: boolean };

export function FlowList({
  tenantId,
  activeId,
  canEdit,
  onSelect,
  onCreated,
}: {
  tenantId: string;
  activeId: string | null;
  canEdit: boolean;
  onSelect: (id: string) => void;
  onCreated: (id: string) => void;
}) {
  const [flows, setFlows] = useState<FlowMeta[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [templates, setTemplates] = useState<TemplateMeta[]>([]);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  // ── overlays: no prompt()/confirm()/alert() ──
  const [newFlow, setNewFlow] = useState<{ defaultName: string; handoff: Handoff; from: string } | null>(null);
  const [nfTeam, setNfTeam] = useState("support");
  const [nfName, setNfName] = useState("");
  const [promptOpen, setPromptOpen] = useState(false);
  const [promptText, setPromptText] = useState("");
  const [promptTeam, setPromptTeam] = useState("support");
  const [manageOpen, setManageOpen] = useState(false);
  const [confirmDel, setConfirmDel] = useState<string | null>(null);

  function reloadTemplates() {
    api.templates.list().then(setTemplates).catch(() => {});
  }
  useEffect(reloadTemplates, []);

  useEffect(() => {
    api
      .listFlows()
      .then(setFlows)
      .catch((e: ApiError) => setErr(e.message));
  }, []);

  function openNewFlow(defaultName: string, handoff: Handoff, from: string) {
    setNfTeam("support");
    setNfName(defaultName);
    setErr(null);
    setNewFlow({ defaultName, handoff, from });
  }

  async function submitNewFlow() {
    if (!newFlow || !nfTeam.trim()) return;
    setBusy(true);
    try {
      const { flow_id } = await api.createFlow({
        team: nfTeam.trim(),
        name: nfName.trim() || newFlow.defaultName,
        tenant_id: tenantId,
      });
      if (newFlow.handoff.candidate) {
        sessionStorage.setItem(`pendingCandidate:${flow_id}`, JSON.stringify(newFlow.handoff.candidate));
      } else if (newFlow.handoff.mermaidPrompt) {
        sessionStorage.setItem(`pendingAssistMode:${flow_id}`, "mermaid");
      }
      setNewFlow(null);
      onCreated(flow_id);
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setBusy(false);
  }

  async function fromTemplate(id: string) {
    if (!id) return;
    try {
      const cand = await api.templates.graph(id);
      openNewFlow(cand.name || "New flow", { candidate: cand }, "template");
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }

  async function submitPrompt() {
    if (!promptText.trim()) return;
    setBusy(true);
    try {
      const res = await api.assistNewFlow(promptText.trim());
      const { flow_id } = await api.createFlow({
        team: promptTeam.trim() || "support",
        name: res.name || "AI flow",
        tenant_id: tenantId,
      });
      sessionStorage.setItem(`pendingCandidate:${flow_id}`, JSON.stringify(res));
      setPromptOpen(false);
      setPromptText("");
      onCreated(flow_id);
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setBusy(false);
  }

  async function removeTemplate(id: string, name: string) {
    setConfirmDel(null);
    try {
      await api.templates.remove(id);
      reloadTemplates();
      toast(`Deleted template "${name}"`);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }

  // the flow list is scoped to the active workspace — /api/flows returns every
  // tenant the caller belongs to (fine for the cross-tenant runs view), so the
  // editor sidebar filters here, like every other tenant-scoped view.
  const visible = flows.filter((f) => f.tenant_id === tenantId);
  const customTemplates = templates.filter((t) => t.source === "custom");
  const handoffNote =
    newFlow?.from === "template"
      ? "Starts from the template draft — review and Save in the editor."
      : newFlow?.from === "mermaid"
        ? "Opens the Mermaid importer once the flow is created."
        : null;

  return (
    <div className="col">
      {canEdit && (
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          <Button variant="secondary" size="sm" onClick={() => openNewFlow("Untitled flow", {}, "blank")}>
            ＋ New flow
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setPromptText("");
              setErr(null);
              setPromptOpen(true);
            }}
            title="describe it in plain English, AI drafts the graph"
          >
            ✨ From prompt
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => openNewFlow("Imported flow", { mermaidPrompt: true }, "mermaid")}
            title="start from a Mermaid flowchart"
          >
            ⬇ From Mermaid
          </Button>
          {templates.length > 0 && (
            <select
              value=""
              title="start from a ready-made flow"
              onChange={(e) => {
                void fromTemplate(e.target.value);
                e.currentTarget.value = "";
              }}
            >
              <option value="">📋 From template…</option>
              {templates.map((t) => (
                <option key={t.id} value={t.id} title={t.description}>
                  {t.name}
                  {t.source === "custom" ? " (custom)" : ""}
                </option>
              ))}
            </select>
          )}
          {customTemplates.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setManageOpen(true)}
              title="delete one of your saved templates"
            >
              🗑 Manage templates
            </Button>
          )}
        </div>
      )}

      {err && (
        <Banner
          tone="exception"
          title={err}
          actions={
            <Button variant="ghost" size="sm" onClick={() => setErr(null)}>
              Dismiss
            </Button>
          }
        />
      )}

      {canEdit && visible.length === 0 && !err && (
        <div
          className="col"
          style={{ gap: 6, border: "1px solid var(--line)", borderRadius: 2, padding: 12, fontSize: 13 }}
        >
          <strong>Get started</strong>
          <ol style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>
            <li>
              Start from a template —{" "}
              <Button variant="ghost" size="sm" onClick={() => fromTemplate("support-autoreply")}>
                Support auto-reply
              </Button>
            </li>
            <li>Add knowledge in the Knowledge tab (upload a file or crawl your docs)</li>
            <li>Open the flow, send a test in the Run panel, then Publish</li>
          </ol>
        </div>
      )}

      {visible.map((f) => (
        <div
          key={f.flow_id}
          className={`flow-item${f.flow_id === activeId ? " active" : ""}`}
          onClick={() => onSelect(f.flow_id)}
        >
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span>{f.team}</span>
            <span className={`pill ${f.status}`}>{f.status}</span>
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            {f.name}
          </div>
        </div>
      ))}
      {visible.length === 0 && !err && <div className="muted">no flows in this workspace</div>}

      <Dialog
        open={!!newFlow}
        onClose={() => setNewFlow(null)}
        title={newFlow?.from === "blank" ? "New flow" : "New flow from a draft"}
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setNewFlow(null) },
          { label: busy ? "Creating…" : "Create", variant: "primary", onClick: submitNewFlow },
        ]}
      >
        <div style={{ display: "grid", gap: 12 }}>
          {handoffNote && <div className="muted" style={{ fontSize: 12 }}>{handoffNote}</div>}
          <Field label="Team" hint="support / csm / offboarding / …">
            <Input value={nfTeam} autoFocus onChange={(e) => setNfTeam(e.target.value)} />
          </Field>
          <Field label="Name">
            <Input
              value={nfName}
              placeholder={newFlow?.defaultName}
              onChange={(e) => setNfName(e.target.value)}
            />
          </Field>
        </div>
      </Dialog>

      <Dialog
        open={promptOpen}
        onClose={() => setPromptOpen(false)}
        title="Describe the flow"
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setPromptOpen(false) },
          { label: busy ? "Generating…" : "Generate", variant: "primary", onClick: submitPrompt },
        ]}
      >
        <div style={{ display: "grid", gap: 12 }}>
          <Field
            label="What should this flow do?"
            hint="e.g. retrieve docs, triage by tier, draft a reply, auto-send only if confident, otherwise ask a human"
          >
            <Textarea
              rows={4}
              autoFocus
              value={promptText}
              onChange={(e) => setPromptText(e.target.value)}
            />
          </Field>
          <Field label="Team">
            <Input value={promptTeam} onChange={(e) => setPromptTeam(e.target.value)} />
          </Field>
        </div>
      </Dialog>

      <Dialog
        open={manageOpen}
        onClose={() => {
          setManageOpen(false);
          setConfirmDel(null);
        }}
        title="Custom templates"
        actions={[{ label: "Close", variant: "secondary", onClick: () => setManageOpen(false) }]}
      >
        <div style={{ display: "grid", gap: 1 }}>
          {customTemplates.length === 0 && <div className="muted">No custom templates saved.</div>}
          {customTemplates.map((t) => (
            <div
              key={t.id}
              className="row"
              style={{ justifyContent: "space-between", padding: "8px 0", borderBottom: "1px solid var(--surface-raised)" }}
            >
              <span title={t.description}>{t.name}</span>
              {confirmDel === t.id ? (
                <span className="row" style={{ gap: 6 }}>
                  <Button variant="ghost" size="sm" onClick={() => setConfirmDel(null)}>
                    Cancel
                  </Button>
                  <Button variant="danger" size="sm" onClick={() => removeTemplate(t.id, t.name)}>
                    Delete
                  </Button>
                </span>
              ) : (
                <Button variant="ghost" size="sm" onClick={() => setConfirmDel(t.id)}>
                  Delete
                </Button>
              )}
            </div>
          ))}
        </div>
      </Dialog>
    </div>
  );
}

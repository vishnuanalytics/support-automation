import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { IntakeChecklist, IntakePreview, IntakeSignal } from "../types";
import { Banner, Button, ConfirmButton, Field, Input, Select, Textarea, Toggle } from "../ui";

/**
 * Editor for `intake_checklists` (migration 101 / interpreter/intake.py).
 *
 * A checklist = when it applies (`match`) + the ordered signals the bot
 * needs to investigate that issue class. Each signal carries the *exact*
 * question to ask when it is missing, a `detect` rule that says when it is
 * already answered (so the bot doesn't ask), and where the answer lands
 * (a Salesforce field, or just the run context). The `clarify` node reads
 * this when its node config has `use_checklists: true`.
 */

type DetectKind = "none" | "any_of" | "regex" | "attachment_type";

function detectKind(d: IntakeSignal["detect"]): DetectKind {
  if (!d) return "none";
  if (d.any_of) return "any_of";
  if (d.regex) return "regex";
  if (d.attachment_type) return "attachment_type";
  return "none";
}

function detectValue(d: IntakeSignal["detect"]): string {
  if (!d) return "";
  if (d.any_of) return d.any_of.join(", ");
  if (d.regex) return d.regex;
  if (d.attachment_type) return d.attachment_type;
  return "";
}

function buildDetect(kind: DetectKind, value: string): IntakeSignal["detect"] {
  const v = value.trim();
  if (kind === "any_of")
    return { any_of: v.split(",").map((s) => s.trim()).filter(Boolean) };
  if (kind === "regex") return v ? { regex: v } : undefined;
  if (kind === "attachment_type")
    return { attachment_type: v === "video" ? "video" : "image" };
  return undefined;
}

const BLANK_SIGNAL: IntakeSignal = {
  key: "",
  label: "",
  question: "",
  required: true,
  lands_in: { ctx: "" },
};

type Draft = {
  checklist_id: string | null;
  label: string;
  priority: number;
  enabled: boolean;
  match: {
    module?: string;
    submodule?: string;
    case_type?: string;
    keywords?: string[];
  };
  signals: IntakeSignal[];
};

function toDraft(c: IntakeChecklist): Draft {
  return {
    checklist_id: c.checklist_id,
    label: c.label,
    priority: c.priority ?? 0,
    enabled: c.enabled ?? true,
    match: {
      module: c.match?.module ?? "",
      submodule: c.match?.submodule ?? "",
      case_type: c.match?.case_type ?? "",
      keywords: c.match?.keywords ?? [],
    },
    signals: (c.signals ?? []).map((s) => ({ ...BLANK_SIGNAL, ...s })),
  };
}

function newDraft(): Draft {
  return {
    checklist_id: null,
    label: "",
    priority: 10,
    enabled: true,
    match: { module: "", submodule: "", case_type: "", keywords: [] },
    signals: [{ ...BLANK_SIGNAL }],
  };
}

function draftToPayload(d: Draft) {
  const match: Draft["match"] = {};
  if (d.match.module?.trim()) match.module = d.match.module.trim();
  if (d.match.submodule?.trim()) match.submodule = d.match.submodule.trim();
  if (d.match.case_type?.trim()) match.case_type = d.match.case_type.trim();
  if (d.match.keywords && d.match.keywords.length) match.keywords = d.match.keywords;
  const signals = d.signals
    .filter((s) => s.key.trim())
    .map((s) => {
      const out: IntakeSignal = {
        key: s.key.trim(),
        label: s.label?.trim() || undefined,
        question: s.question?.trim() || undefined,
        required: s.required ?? true,
      };
      if (s.detect && detectKind(s.detect) !== "none") out.detect = s.detect;
      const dest = s.lands_in?.sf_field?.trim()
        ? { sf_field: s.lands_in.sf_field.trim() }
        : { ctx: (s.lands_in?.ctx?.trim() || s.key.trim()) };
      out.lands_in = dest;
      if (s.vision) out.vision = true;
      return out;
    });
  return { label: d.label.trim(), priority: d.priority, enabled: d.enabled, match, signals };
}

export function IntakeView({ tenantId }: { tenantId: string }) {
  const [rows, setRows] = useState<IntakeChecklist[]>([]);
  const [sel, setSel] = useState<string | "new" | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setRows(await api.intake.list(tenantId));
      setErr(null);
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }, [tenantId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    setNote(null);
    if (sel === "new") setDraft(newDraft());
    else if (sel) {
      const row = rows.find((r) => r.checklist_id === sel);
      setDraft(row ? toDraft(row) : null);
    } else setDraft(null);
  }, [sel, rows]);

  const patch = (p: Partial<Draft>) => setDraft((d) => (d ? { ...d, ...p } : d));
  const patchMatch = (p: Partial<Draft["match"]>) =>
    setDraft((d) => (d ? { ...d, match: { ...d.match, ...p } } : d));
  const patchSignal = (i: number, p: Partial<IntakeSignal>) =>
    setDraft((d) =>
      d ? { ...d, signals: d.signals.map((s, j) => (j === i ? { ...s, ...p } : s)) } : d,
    );
  const moveSignal = (i: number, dir: -1 | 1) =>
    setDraft((d) => {
      if (!d) return d;
      const j = i + dir;
      if (j < 0 || j >= d.signals.length) return d;
      const s = [...d.signals];
      [s[i], s[j]] = [s[j], s[i]];
      return { ...d, signals: s };
    });

  async function save() {
    if (!draft) return;
    const body = draftToPayload(draft);
    if (!body.label) {
      setErr("give the checklist a name");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      if (draft.checklist_id) {
        await api.intake.update(draft.checklist_id, body);
        setNote("saved");
      } else {
        const created = await api.intake.create({ ...body, tenant_id: tenantId });
        setNote("created");
        setSel(created.checklist_id);
      }
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!draft?.checklist_id) return;
    try {
      await api.intake.remove(draft.checklist_id);
      setSel(null);
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  return (
    <div className="col" style={{ gap: "var(--space-3)", padding: "var(--space-3)" }}>
      <div>
        <h2 style={{ margin: 0 }}>Intake checklists</h2>
        <div className="muted" style={{ fontSize: 13, marginTop: 4, maxWidth: 720 }}>
          What the bot must find out before it can act on a class of issue. When a
          customer's report is too thin, the <code>clarify</code> node asks the
          missing questions from the matching checklist (and skips anything the
          message already answers). Turn it on per flow with the clarify node's{" "}
          <code>use_checklists</code> option.
        </div>
      </div>

      {err && <Banner tone="exception" title={err} />}
      {note && <Banner tone="accent" title={note} />}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(220px, 280px) minmax(0, 1fr)",
          gap: "var(--space-4)",
          alignItems: "start",
        }}
      >
        {/* list */}
        <div className="col" style={{ gap: 4 }}>
          <Button size="sm" variant="primary" onClick={() => setSel("new")}>
            + New checklist
          </Button>
          {rows.length === 0 && (
            <div className="muted" style={{ fontSize: 12, padding: "var(--space-2) 0" }}>
              none yet
            </div>
          )}
          {rows.map((r) => {
            const req = r.signals.filter((s) => s.required ?? true).length;
            return (
              <button
                key={r.checklist_id}
                className={"nav-item" + (sel === r.checklist_id ? " active" : "")}
                style={{ textAlign: "left", height: "auto", padding: "var(--space-2)" }}
                onClick={() => setSel(r.checklist_id)}
              >
                <div className="col" style={{ gap: 2, width: "100%" }}>
                  <span style={{ fontWeight: 600, display: "flex", gap: 6, alignItems: "center" }}>
                    <span
                      aria-hidden
                      style={{
                        width: 7,
                        height: 7,
                        borderRadius: "50%",
                        background: r.enabled ? "var(--accent)" : "var(--line-strong)",
                        flex: "none",
                      }}
                    />
                    {r.label}
                  </span>
                  <span className="muted" style={{ fontSize: 11 }}>
                    priority {r.priority} · {req}/{r.signals.length} required
                  </span>
                </div>
              </button>
            );
          })}
        </div>

        {/* editor */}
        {draft ? (
          <div className="col" style={{ gap: "var(--space-3)", minWidth: 0 }}>
            <div className="row" style={{ gap: "var(--space-2)", flexWrap: "wrap", alignItems: "flex-end" }}>
              <Field label="Checklist name">
                <Input
                  style={{ minWidth: 280 }}
                  value={draft.label}
                  placeholder="Menu images not visible on delivery channels"
                  onChange={(e) => patch({ label: e.target.value })}
                />
              </Field>
              <Field label="Priority" hint="higher wins when two match">
                <Input
                  type="number"
                  style={{ width: 90 }}
                  value={draft.priority}
                  onChange={(e) => patch({ priority: Number(e.target.value) || 0 })}
                />
              </Field>
              <Toggle checked={draft.enabled} onChange={(v) => patch({ enabled: v })}>
                <span style={{ fontSize: 13 }}>Enabled</span>
              </Toggle>
              <div style={{ flex: 1 }} />
              <Button variant="primary" loading={busy} onClick={save}>
                {draft.checklist_id ? "Save" : "Create"}
              </Button>
              {draft.checklist_id && (
                <ConfirmButton
                  label="Delete"
                  title={`Delete "${draft.label}"?`}
                  body="This removes the checklist for the whole workspace."
                  confirmLabel="Delete checklist"
                  onConfirm={remove}
                />
              )}
            </div>

            {/* match */}
            <section className="col" style={{ gap: "var(--space-2)" }}>
              <h3 style={{ margin: 0, font: "var(--type-section)" }}>When to use this</h3>
              <div className="muted" style={{ fontSize: 12 }}>
                All the conditions you set must hold. The checklist matching the most
                conditions wins. Leave a field blank to ignore it.
              </div>
              <div className="row" style={{ gap: "var(--space-2)", flexWrap: "wrap" }}>
                <Field label="Module">
                  <Input
                    style={{ width: 170 }}
                    value={draft.match.module ?? ""}
                    onChange={(e) => patchMatch({ module: e.target.value })}
                  />
                </Field>
                <Field label="Sub-module">
                  <Input
                    style={{ width: 170 }}
                    value={draft.match.submodule ?? ""}
                    onChange={(e) => patchMatch({ submodule: e.target.value })}
                  />
                </Field>
                <Field label="Case type">
                  <Input
                    style={{ width: 170 }}
                    value={draft.match.case_type ?? ""}
                    placeholder="Problem / Bug"
                    onChange={(e) => patchMatch({ case_type: e.target.value })}
                  />
                </Field>
              </div>
              <Field
                label="Keywords"
                hint="comma-separated — ANY hit in the subject, body or topic counts"
              >
                <Textarea
                  rows={2}
                  value={(draft.match.keywords ?? []).join(", ")}
                  placeholder="image, images, not visible, not showing"
                  onChange={(e) =>
                    patchMatch({
                      keywords: e.target.value
                        .split(",")
                        .map((s) => s.trim())
                        .filter(Boolean),
                    })
                  }
                />
              </Field>
            </section>

            {/* signals */}
            <section className="col" style={{ gap: "var(--space-2)" }}>
              <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                <h3 style={{ margin: 0, font: "var(--type-section)" }}>Questions to ask</h3>
                <Button
                  size="sm"
                  onClick={() => patch({ signals: [...draft.signals, { ...BLANK_SIGNAL }] })}
                >
                  + Add question
                </Button>
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                Each row is one detail the bot needs. It only asks the ones it can't
                already find. Order = the order questions are asked.
              </div>
              {draft.signals.map((s, i) => (
                <SignalRow
                  key={i}
                  index={i}
                  total={draft.signals.length}
                  signal={s}
                  onChange={(p) => patchSignal(i, p)}
                  onMove={(dir) => moveSignal(i, dir)}
                  onRemove={() =>
                    patch({ signals: draft.signals.filter((_, j) => j !== i) })
                  }
                />
              ))}
            </section>

            <PreviewPanel tenantId={tenantId} draftKeywords={draft.match.keywords ?? []} />
          </div>
        ) : (
          <div className="muted" style={{ fontSize: 13, paddingTop: "var(--space-3)" }}>
            Pick a checklist on the left, or create one.
          </div>
        )}
      </div>
    </div>
  );
}

function SignalRow({
  index,
  total,
  signal,
  onChange,
  onMove,
  onRemove,
}: {
  index: number;
  total: number;
  signal: IntakeSignal;
  onChange: (p: Partial<IntakeSignal>) => void;
  onMove: (dir: -1 | 1) => void;
  onRemove: () => void;
}) {
  const dk = detectKind(signal.detect);
  const dv = detectValue(signal.detect);
  const landsSf = !!signal.lands_in?.sf_field;

  return (
    <div
      className="col"
      style={{
        gap: "var(--space-2)",
        padding: "var(--space-2)",
        border: "1px solid var(--line)",
        borderRadius: "var(--radius-md)",
        background: "var(--surface-raised)",
      }}
    >
      <div className="row" style={{ gap: "var(--space-2)", alignItems: "flex-end", flexWrap: "wrap" }}>
        <Field label={`#${index + 1} key`}>
          <Input
            style={{ width: 140 }}
            value={signal.key}
            placeholder="outlet_ref"
            onChange={(e) => onChange({ key: e.target.value })}
          />
        </Field>
        <Field label="Internal label">
          <Input
            style={{ width: 200 }}
            value={signal.label ?? ""}
            placeholder="Outlet / store identifier"
            onChange={(e) => onChange({ label: e.target.value })}
          />
        </Field>
        <Toggle checked={signal.required ?? true} onChange={(v) => onChange({ required: v })}>
          <span style={{ fontSize: 13 }}>Required</span>
        </Toggle>
        <Toggle checked={!!signal.vision} onChange={(v) => onChange({ vision: v })}>
          <span style={{ fontSize: 13 }}>Read from image</span>
        </Toggle>
        <div style={{ flex: 1 }} />
        <button
          className="ui-btn ui-btn--ghost ui-btn--sm"
          disabled={index === 0}
          onClick={() => onMove(-1)}
          title="move up"
        >
          ↑
        </button>
        <button
          className="ui-btn ui-btn--ghost ui-btn--sm"
          disabled={index === total - 1}
          onClick={() => onMove(1)}
          title="move down"
        >
          ↓
        </button>
        <button
          className="ui-btn ui-btn--ghost ui-btn--sm"
          onClick={onRemove}
          title="remove"
        >
          ✕
        </button>
      </div>

      <Field label="Question the bot sends the customer">
        <Textarea
          rows={2}
          value={signal.question ?? ""}
          placeholder="What is the outlet ID (or the exact store name and city) where images are missing?"
          onChange={(e) => onChange({ question: e.target.value })}
        />
      </Field>

      <div className="row" style={{ gap: "var(--space-2)", flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label="Already answered when" hint="skip the question if this hits">
          <Select
            value={dk}
            options={[
              { value: "none", label: "— always ask —" },
              { value: "any_of", label: "text contains any of" },
              { value: "regex", label: "text matches regex" },
              { value: "attachment_type", label: "an attachment of type" },
            ]}
            onChange={(e) =>
              onChange({ detect: buildDetect(e.target.value as DetectKind, dv) })
            }
          />
        </Field>
        {dk !== "none" && (
          <Field label={dk === "attachment_type" ? "type" : "value"}>
            {dk === "attachment_type" ? (
              <Select
                value={dv || "image"}
                options={[
                  { value: "image", label: "image" },
                  { value: "video", label: "video" },
                ]}
                onChange={(e) => onChange({ detect: buildDetect(dk, e.target.value) })}
              />
            ) : (
              <Input
                style={{ width: 280 }}
                value={dv}
                placeholder={dk === "any_of" ? "swiggy, zomato, ubereats" : "\\b[45]\\d\\d\\b"}
                onChange={(e) => onChange({ detect: buildDetect(dk, e.target.value) })}
              />
            )}
          </Field>
        )}
      </div>

      <div className="row" style={{ gap: "var(--space-2)", flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label="Answer lands in">
          <Select
            value={landsSf ? "sf_field" : "ctx"}
            options={[
              { value: "ctx", label: "run context only" },
              { value: "sf_field", label: "a Salesforce field" },
            ]}
            onChange={(e) =>
              onChange(
                e.target.value === "sf_field"
                  ? { lands_in: { sf_field: signal.lands_in?.sf_field ?? "" } }
                  : { lands_in: { ctx: signal.lands_in?.ctx ?? signal.key } },
              )
            }
          />
        </Field>
        <Field label={landsSf ? "Field API name" : "Context key"}>
          <Input
            style={{ width: 220 }}
            value={landsSf ? signal.lands_in?.sf_field ?? "" : signal.lands_in?.ctx ?? ""}
            placeholder={landsSf ? "SubModule__c" : "outlet_ref"}
            onChange={(e) =>
              onChange({
                lands_in: landsSf
                  ? { sf_field: e.target.value }
                  : { ctx: e.target.value },
              })
            }
          />
        </Field>
      </div>
    </div>
  );
}

function PreviewPanel({
  tenantId,
  draftKeywords,
}: {
  tenantId: string;
  draftKeywords: string[];
}) {
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [caseType, setCaseType] = useState("");
  const [res, setRes] = useState<IntakePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const hint = useMemo(
    () => (draftKeywords.length ? `try a message mentioning: ${draftKeywords.slice(0, 4).join(", ")}` : ""),
    [draftKeywords],
  );

  async function run() {
    setBusy(true);
    setErr(null);
    try {
      setRes(await api.intake.preview({ subject, body, case_type: caseType, tenant_id: tenantId }));
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="col"
      style={{
        gap: "var(--space-2)",
        padding: "var(--space-3)",
        border: "1px solid var(--line)",
        borderRadius: "var(--radius-md)",
      }}
    >
      <h3 style={{ margin: 0, font: "var(--type-section)" }}>Preview</h3>
      <div className="muted" style={{ fontSize: 12 }}>
        Paste a sample case. This runs the real matcher + extractor against your
        saved checklists and shows the exact questions the bot would ask.
        {hint ? ` — ${hint}` : ""}
      </div>
      <div className="row" style={{ gap: "var(--space-2)", flexWrap: "wrap" }}>
        <Field label="Subject">
          <Input
            style={{ width: 260 }}
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
          />
        </Field>
        <Field label="Case type">
          <Input
            style={{ width: 160 }}
            value={caseType}
            placeholder="Problem / Bug"
            onChange={(e) => setCaseType(e.target.value)}
          />
        </Field>
      </div>
      <Field label="Body">
        <Textarea rows={3} value={body} onChange={(e) => setBody(e.target.value)} />
      </Field>
      <div>
        <Button size="sm" loading={busy} onClick={run}>
          Run preview
        </Button>
      </div>

      {err && <Banner tone="exception" title={err} />}

      {res && (
        <div className="col" style={{ gap: 6 }}>
          <div style={{ fontSize: 13 }}>
            matched:{" "}
            {res.matched ? (
              <strong>{res.matched}</strong>
            ) : (
              <span className="muted">no checklist matched</span>
            )}
          </div>
          {res.questions.length > 0 && (
            <div className="col" style={{ gap: 2 }}>
              <span className="muted" style={{ fontSize: 12 }}>
                questions the bot would send:
              </span>
              <ol style={{ margin: 0, paddingLeft: 18 }}>
                {res.questions.map((q, i) => (
                  <li key={i} style={{ fontSize: 13 }}>
                    {q}
                  </li>
                ))}
              </ol>
            </div>
          )}
          {Object.keys(res.known).length > 0 && (
            <div className="muted" style={{ fontSize: 12 }}>
              already known:{" "}
              {Object.entries(res.known)
                .map(([k, v]) => `${k}=${v} (${res.sources[k] ?? "?"})`)
                .join(" · ")}
            </div>
          )}
          {Object.keys(res.field_writes).length > 0 && (
            <div className="muted" style={{ fontSize: 12 }}>
              would write:{" "}
              {Object.entries(res.field_writes)
                .map(([k, v]) => `${k} = ${v}`)
                .join(" · ")}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

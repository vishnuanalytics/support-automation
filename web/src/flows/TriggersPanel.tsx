import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { FlowTrigger } from "../types";

/** P6a — webhook / schedule triggers for a flow. A collapsible strip shown
 *  under the editor toolbar; collapsed by default to a one-line summary so
 *  it doesn't compete with the canvas for space. */
export function TriggersPanel({ flowId, canEdit }: { flowId: string; canEdit: boolean }) {
  const [rows, setRows] = useState<FlowTrigger[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [cron, setCron] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  const load = () => {
    api.triggers.list(flowId).then(setRows).catch((e: ApiError) => setErr(e.message));
  };
  useEffect(load, [flowId]);

  const add = async (kind: "webhook" | "schedule") => {
    setBusy(true);
    setErr(null);
    try {
      await api.triggers.create(flowId, kind === "schedule" ? { kind, cron } : { kind });
      setCron("");
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const remove = async (id: string) => {
    await api.triggers.remove(flowId, id).catch(() => {});
    load();
  };

  if (rows === null) return null;

  const webhookCount = rows.filter((r) => r.kind === "webhook").length;
  const scheduleCount = rows.filter((r) => r.kind === "schedule").length;
  const summary =
    rows.length === 0
      ? "no triggers — this flow only runs when called from another flow or case event"
      : `${webhookCount} webhook${webhookCount === 1 ? "" : "s"} · ${scheduleCount} schedule${scheduleCount === 1 ? "" : "s"}`;

  return (
    <div style={{ borderTop: "1px solid var(--hair, #ddd)", fontSize: 13 }}>
      <button
        className="nav-group-header"
        style={{ width: "100%", padding: "6px 10px", textTransform: "none", letterSpacing: 0, fontSize: 13 }}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="nav-caret">{open ? "▾" : "▸"}</span> Triggers: {summary}
      </button>
      {open && (
        <div
          className="row"
          style={{
            gap: 10,
            flexWrap: "wrap",
            alignItems: "center",
            padding: "6px 10px 10px",
          }}
        >
          <div className="muted" style={{ flexBasis: "100%", fontSize: 12 }}>
            Start this flow from outside the editor: a webhook gives you a URL to
            POST a case payload to (e.g. from Zapier or a CRM); a schedule runs it
            on a cron interval.
          </div>
          {rows.length === 0 && <span className="muted">none yet</span>}
          {rows.map((t) => (
            <span
              key={t.trigger_id}
              className="row"
              style={{
                gap: 6,
                alignItems: "center",
                background: "var(--surface-2, #eee)",
                borderRadius: 6,
                padding: "2px 8px",
              }}
            >
              <b>{t.kind}</b>
              {t.kind === "webhook" && t.url && (
                <>
                  <code
                    title={t.url}
                    style={{
                      display: "inline-block",
                      maxWidth: 340,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      verticalAlign: "bottom",
                    }}
                  >
                    {t.url}
                  </code>
                  <button title="copy" onClick={() => navigator.clipboard?.writeText(t.url!)}>
                    ⧉
                  </button>
                </>
              )}
              {t.kind === "schedule" && <code>{t.cron}</code>}
              <span className="muted">· {t.fire_count} fired</span>
              {canEdit && (
                <button className="err" title="delete" onClick={() => remove(t.trigger_id)}>
                  ×
                </button>
              )}
            </span>
          ))}
          {canEdit && (
            <>
              <button disabled={busy} onClick={() => add("webhook")}>
                + webhook
              </button>
              <input
                placeholder="*/15 * * * *"
                value={cron}
                onChange={(e) => setCron(e.target.value)}
                style={{ width: 110 }}
              />
              <button disabled={busy || !cron.trim()} onClick={() => add("schedule")}>
                + schedule
              </button>
            </>
          )}
          {err && <span className="err">{err}</span>}
        </div>
      )}
    </div>
  );
}

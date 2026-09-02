import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { FlowTrigger } from "../types";

/** P6a — webhook / schedule triggers for a flow. A compact strip shown under
 *  the editor toolbar. */
export function TriggersPanel({ flowId, canEdit }: { flowId: string; canEdit: boolean }) {
  const [rows, setRows] = useState<FlowTrigger[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [cron, setCron] = useState("");
  const [busy, setBusy] = useState(false);

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

  return (
    <div
      className="row"
      style={{
        gap: 10,
        flexWrap: "wrap",
        alignItems: "center",
        padding: "6px 10px",
        borderTop: "1px solid var(--hair, #ddd)",
        fontSize: 13,
      }}
    >
      <span className="muted" style={{ fontFamily: "ui-monospace, monospace" }}>
        triggers
      </span>
      {rows.length === 0 && <span className="muted">none</span>}
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
              <code style={{ maxWidth: 340, overflow: "hidden", textOverflow: "ellipsis" }}>
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
  );
}

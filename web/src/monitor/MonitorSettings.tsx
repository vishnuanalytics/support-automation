import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, Skeleton, Textarea, Toggle } from "../ui";

type ScanKey = "auto_checks_dsp" | "auto_checks_engineering";

const SCANS: { key: ScanKey; label: string; hint: string }[] = [
  { key: "auto_checks_dsp", label: "New aggregator (DSP) cases",
    hint: "Run the saved order, stock, store and menu checks on aggregator cases raised from the SLA start." },
  { key: "auto_checks_engineering", label: "New engineering cases",
    hint: "Run them on cases raised to engineering too, and judge those raises on the Agents page. A case that has its own aggregator case is checked there instead." },
];

function parse(text: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(text || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

export function MonitorSettings({ tenantId }: { tenantId?: string }) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.monitor.getConfig(tenantId)
      .then((r) => setText(JSON.stringify(r.config, null, 2)))
      .catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  const put = async (config: Record<string, unknown>, message: string) => {
    setErr(null);
    setSaved(null);
    setBusy(true);
    try {
      const r = await api.monitor.putConfig(config, tenantId);
      setText(JSON.stringify(r.config, null, 2));
      setSaved(message);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    const config = parse(text || "{}");
    if (!config) return setErr("That isn't valid JSON");
    return put(config, "Settings saved");
  };

  if (text === null) return err ? <Banner tone="exception" title={err} /> : <Skeleton lines={8} />;
  const config = parse(text);
  const rules = Array.isArray(config?.auto_checks) ? config.auto_checks.length : 0;
  return (
    <section className="monitor-settings">
      <h2>Monitor settings</h2>
      {err && <Banner tone="exception" title={err} />}
      {saved && <Banner tone="success" title={saved} />}

      <div className="monitor-scans">
        <h3>Automatic data checks</h3>
        <p className="monitor-muted">
          {rules
            ? `${rules} rule${rules === 1 ? "" : "s"} in auto_checks pick which checks a case gets. `
            : "No auto_checks rules yet, so nothing runs until you add some below. "}
          The hourly routine runs them and saves the results on the case.
        </p>
        {!config && <p className="monitor-muted">Fix the JSON below to change these.</p>}
        {SCANS.map((s) => {
          const on = config?.[s.key] !== undefined ? config[s.key] === true : s.key === "auto_checks_dsp";
          return (
            <div key={s.key} className="monitor-scan">
              <Toggle checked={on} disabled={!config || busy}
                      onChange={(v) => config && put({ ...config, [s.key]: v },
                        `${s.label}: checks ${v ? "on" : "off"}`)}>
                {s.label}
              </Toggle>
              <span className="monitor-muted">{s.hint}</span>
            </div>
          );
        })}
      </div>

      <h3>All settings</h3>
      <p className="monitor-muted">
        Map your case system's fields and waiting values here. Set <code>enabled</code> to true to
        start scheduled syncs.
      </p>
      <Textarea aria-label="Monitor settings JSON" rows={24} value={text}
                onChange={(e) => setText(e.target.value)} />
      <Button variant="primary" onClick={save} disabled={busy}>Save settings</Button>
    </section>
  );
}

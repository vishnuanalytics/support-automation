import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, Skeleton, Textarea } from "../ui";

export function MonitorSettings({ tenantId }: { tenantId?: string }) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api.monitor.getConfig(tenantId)
      .then((r) => setText(JSON.stringify(r.config, null, 2)))
      .catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  const save = async () => {
    setErr(null);
    setSaved(false);
    try {
      await api.monitor.putConfig(JSON.parse(text || "{}"), tenantId);
      setSaved(true);
    } catch (e) {
      setErr(e instanceof SyntaxError ? "That isn't valid JSON" : (e as ApiError).message);
    }
  };

  if (text === null) return err ? <Banner tone="exception" title={err} /> : <Skeleton lines={8} />;
  return (
    <section className="monitor-settings">
      <h2>Monitor settings</h2>
      <p className="monitor-muted">
        Map your case system's fields and waiting values here. Set <code>enabled</code> to true to
        start scheduled syncs.
      </p>
      {err && <Banner tone="exception" title={err} />}
      {saved && <Banner tone="success" title="Settings saved" />}
      <Textarea aria-label="Monitor settings JSON" rows={24} value={text}
                onChange={(e) => setText(e.target.value)} />
      <Button variant="primary" onClick={save}>Save settings</Button>
    </section>
  );
}

import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { BillingUsage, FlowCostDelta } from "../types";
import { Button, Tag, Banner, StatTile, QuotaBar } from "../ui";

function shiftPeriod(period: string, delta: number): string {
  const [y, m] = period.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + delta, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function BillingView({ tenantId }: { tenantId: string }) {
  const currentPeriod = useMemo(() => {
    const now = new Date();
    return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
  }, []);
  const [period, setPeriod] = useState(currentPeriod);
  const [usage, setUsage] = useState<BillingUsage | null>(null);
  const [deltas, setDeltas] = useState<FlowCostDelta[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setErr(null);
    api
      .billingUsage({ period, tenantId })
      .then(setUsage)
      .catch((e: ApiError) => setErr(e.message));
    api.billingFlowDeltas(tenantId).then(setDeltas).catch(() => {});
  }, [period, tenantId]);

  const maxDailyTokens = Math.max(1, ...(usage?.daily.map((d) => d.tokens) ?? [0]));

  return (
    <div className="billing-view">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <div className="row" style={{ gap: 6 }}>
          <Button variant="ghost" size="sm" onClick={() => setPeriod((p) => shiftPeriod(p, -1))}>
            ← prev
          </Button>
          <strong style={{ font: "var(--type-mono)" }}>{period}</strong>
          <Button
            variant="ghost"
            size="sm"
            disabled={period === currentPeriod}
            onClick={() => setPeriod((p) => shiftPeriod(p, 1))}
          >
            next →
          </Button>
        </div>
        {usage && <Tag tone="neutral">{usage.plan} plan</Tag>}
      </div>

      {err && (
        <Banner
          tone="exception"
          title={err}
          detail={err.toLowerCase().includes("owner") ? "Only a workspace owner can view billing." : undefined}
        />
      )}

      {deltas.filter((d) => (d.ratio ?? 0) > 1.5).length > 0 && (
        <Banner
          tone="warn"
          title="Cost per run jumped after a recent edit"
          detail={
            <div style={{ display: "grid", gap: 2 }}>
              {deltas
                .filter((d) => (d.ratio ?? 0) > 1.5)
                .map((d) => (
                  <div key={d.flow_id} style={{ fontSize: 12 }}>
                    {d.name}: ×{d.ratio!.toFixed(1)} tokens/run since {String(d.edited_at).slice(0, 10)} (
                    {d.before_avg_tokens.toLocaleString()} → {d.after_avg_tokens.toLocaleString()},{" "}
                    {d.runs_before}/{d.runs_after} runs before/after)
                  </div>
                ))}
            </div>
          }
        />
      )}

      {usage && (
        <>
          <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
            <StatTile label="runs" value={usage.runs_count} />
            <StatTile label="tokens" value={usage.tokens_total.toLocaleString()} />
            <StatTile label="est. cost" value={`$${usage.estimated_cost_usd.toFixed(2)}`} tone="accent" />
          </div>

          <div style={{ display: "grid", gap: 16, maxWidth: 520 }}>
            <QuotaLine label="runs" used={usage.billable_runs_count} limit={usage.limits.runs} />
            <QuotaLine label="tokens" used={usage.billable_tokens_total} limit={usage.limits.tokens} />
          </div>
          {(usage.runs_count > usage.billable_runs_count) && (
            <div className="muted" style={{ fontSize: 12 }}>
              {usage.runs_count - usage.billable_runs_count} of these ran entirely on your own LLM
              key — not counted toward your plan.
            </div>
          )}

          <h5>daily usage (tokens)</h5>
          <div className="usage-bars">
            {usage.daily.length === 0 && <div className="muted">no runs this period</div>}
            {usage.daily.map((d) => (
              <div
                key={d.date}
                className="usage-bar"
                title={`${d.date}: ${d.runs} run(s), ${d.tokens.toLocaleString()} tokens`}
                style={{ height: `${Math.max(4, (d.tokens / maxDailyTokens) * 100)}%` }}
              />
            ))}
          </div>

          {usage.by_flow.length > 0 && (
            <>
              <h5>by flow</h5>
              <table className="runs-table">
                <thead>
                  <tr>
                    <th>flow</th>
                    <th>runs</th>
                    <th>tokens</th>
                    <th>est. cost</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.by_flow.map((f) => (
                    <tr key={f.flow_id}>
                      <td>{f.name}</td>
                      <td className="muted">{f.runs.toLocaleString()}</td>
                      <td className="muted">{f.tokens.toLocaleString()}</td>
                      <td className="muted">${f.estimated_cost_usd.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {usage.by_node.length > 0 && (
            <>
              <h5>by node type</h5>
              <table className="runs-table">
                <thead>
                  <tr>
                    <th>node</th>
                    <th>tokens</th>
                    <th>est. cost</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.by_node.map((n) => (
                    <tr key={n.node}>
                      <td>{n.node}</td>
                      <td className="muted">{n.tokens.toLocaleString()}</td>
                      <td className="muted">${n.estimated_cost_usd.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {Object.keys(usage.tokens_by_model).length > 0 && (
            <>
              <h5>tokens by model</h5>
              <div className="col" style={{ gap: 2 }}>
                {Object.entries(usage.tokens_by_model)
                  .sort((a, b) => b[1] - a[1])
                  .map(([model, n]) => (
                    <div key={model} className="row" style={{ justifyContent: "space-between", fontSize: 12 }}>
                      <span className="muted">{model}</span>
                      <span>{n.toLocaleString()}</span>
                    </div>
                  ))}
              </div>
            </>
          )}

          <div className="muted" style={{ fontSize: 11 }}>
            estimated cost is illustrative list pricing, not a real invoice — no payment processing is
            wired up yet.
          </div>
        </>
      )}
    </div>
  );
}

/** Wraps the shared QuotaBar; a null limit reads as "unlimited". */
function QuotaLine({ label, used, limit }: { label: string; used: number; limit: number | null }) {
  if (limit == null) {
    return (
      <div className="muted" style={{ fontSize: 12 }}>
        {label}: {used.toLocaleString()} (unlimited)
      </div>
    );
  }
  return <QuotaBar label={label} used={used} limit={limit} />;
}

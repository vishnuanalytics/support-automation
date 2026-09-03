import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { BillingUsage } from "../types";

function shiftPeriod(period: string, delta: number): string {
  const [y, m] = period.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + delta, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function BillingView() {
  const currentPeriod = useMemo(() => {
    const now = new Date();
    return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
  }, []);
  const [period, setPeriod] = useState(currentPeriod);
  const [usage, setUsage] = useState<BillingUsage | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setErr(null);
    api
      .billingUsage({ period })
      .then(setUsage)
      .catch((e: ApiError) => setErr(e.message));
  }, [period]);

  const maxDailyTokens = Math.max(1, ...(usage?.daily.map((d) => d.tokens) ?? [0]));

  return (
    <div className="billing-view col">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <div className="row" style={{ gap: 6 }}>
          <button onClick={() => setPeriod((p) => shiftPeriod(p, -1))}>← prev</button>
          <strong>{period}</strong>
          <button disabled={period === currentPeriod} onClick={() => setPeriod((p) => shiftPeriod(p, 1))}>
            next →
          </button>
        </div>
        {usage && <span className="pill">{usage.plan} plan</span>}
      </div>

      {err && (
        <div className="banner err">
          {err}
          {err.toLowerCase().includes("owner") && (
            <> — only a workspace owner can view billing.</>
          )}
        </div>
      )}

      {usage && (
        <>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            <div className="tile">
              <div className="v">{usage.runs_count}</div>
              <div className="l">runs</div>
            </div>
            <div className="tile">
              <div className="v">{usage.tokens_total.toLocaleString()}</div>
              <div className="l">tokens</div>
            </div>
            <div className="tile">
              <div className="v">${usage.estimated_cost_usd.toFixed(2)}</div>
              <div className="l">est. cost (illustrative)</div>
            </div>
          </div>

          <QuotaBar label="runs" used={usage.runs_count} limit={usage.limits.runs} pct={usage.pct_runs_used} />
          <QuotaBar
            label="tokens"
            used={usage.tokens_total}
            limit={usage.limits.tokens}
            pct={usage.pct_tokens_used}
          />

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

function QuotaBar({
  label,
  used,
  limit,
  pct,
}: {
  label: string;
  used: number;
  limit: number | null;
  pct: number | null;
}) {
  if (limit == null || pct == null) {
    return (
      <div className="quota-row">
        <div className="muted" style={{ fontSize: 12 }}>
          {label}: {used.toLocaleString()} (unlimited)
        </div>
      </div>
    );
  }
  const cls = pct >= 100 ? "err" : pct >= 80 ? "warn" : "ok";
  return (
    <div className="quota-row">
      <div className="muted" style={{ fontSize: 12 }}>
        {label}: {used.toLocaleString()} / {limit.toLocaleString()} ({pct}%)
      </div>
      <div className="quota-bar">
        <div className={`quota-bar-fill ${cls}`} style={{ width: `${Math.min(100, pct)}%` }} />
      </div>
    </div>
  );
}

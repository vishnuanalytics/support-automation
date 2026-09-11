import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { BillingUsage, FlowCostDelta, Plan } from "../types";
import { Button, Tag, Banner, StatTile, QuotaBar, Select } from "../ui";

function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  return Math.ceil((new Date(iso).getTime() - Date.now()) / 86_400_000);
}

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

      {usage && <BillingStatusBanner state={usage.billing_state} />}

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
            estimated cost above is illustrative list pricing for LLM usage, not your subscription
            invoice — that's billed separately by {usage.billing_state.payment_provider || "your plan's payment provider"}.
          </div>

          <PlansSection
            tenantId={tenantId}
            currentPlan={usage.plan}
            billingCountry={usage.billing_state.billing_country}
          />
        </>
      )}
    </div>
  );
}

function BillingStatusBanner({ state }: { state: BillingUsage["billing_state"] }) {
  if (!state.billing_status || state.billing_status === "active") return null;

  if (state.billing_status === "trialing") {
    const days = daysUntil(state.trial_ends_at);
    return (
      <Banner
        tone="accent"
        title={days != null && days >= 0 ? `Trial — ${days} day${days === 1 ? "" : "s"} left` : "Trial active"}
        detail="Free credits included until the trial ends or your plan's usage cap is hit, whichever comes first — add your own LLM key in Connections to keep testing past the cap, or add a payment method below to keep flows running once the trial itself ends."
      />
    );
  }
  if (state.billing_status === "grace") {
    const days = daysUntil(state.grace_ends_at);
    return (
      <Banner
        tone="warn"
        title="Trial ended — payment needed"
        detail={
          days != null && days >= 0
            ? `Flows pause in ${days} day${days === 1 ? "" : "s"} without a payment method — choose a plan below.`
            : "Choose a plan below to keep flows running."
        }
      />
    );
  }
  if (state.billing_status === "locked") {
    return (
      <Banner
        tone="exception"
        title="Flows are paused"
        detail="No payment method was added before the grace period ended. Choose a plan below to resume immediately."
      />
    );
  }
  if (state.billing_status === "canceled") {
    return (
      <Banner tone="warn" title="Subscription canceled" detail="Choose a plan below to reactivate." />
    );
  }
  return null;
}

function PlansSection({
  tenantId,
  currentPlan,
  billingCountry,
}: {
  tenantId: string;
  currentPlan: string;
  billingCountry: string | null;
}) {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [country, setCountry] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.billingPlans(tenantId).then(setPlans).catch(() => {});
  }, [tenantId]);

  const subscribe = async (slug: string) => {
    setErr(null);
    if (!billingCountry && !country) {
      setErr("Choose a billing country first — it decides which payment provider you'll use.");
      return;
    }
    setBusy(slug);
    try {
      const res = await api.billingSubscribe({
        planSlug: slug,
        tenantId,
        billingCountry: billingCountry ? undefined : country,
      });
      window.location.href = res.checkout_url;
    } catch (e) {
      setErr((e as ApiError).message);
      setBusy(null);
    }
  };

  const paid = plans.filter((p) => p.slug !== "free" && p.slug !== "pro");
  if (paid.length === 0) return null;

  return (
    <div className="col" style={{ gap: 12, marginTop: 8 }}>
      <h5 style={{ margin: 0 }}>Plans</h5>
      {err && <Banner tone="exception" title={err} />}
      {!billingCountry && (
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <span className="muted" style={{ fontSize: 13 }}>
            Billing country
          </span>
          <Select
            options={[
              { value: "IN", label: "India" },
              { value: "US", label: "Everywhere else" },
            ]}
            placeholder="Choose…"
            value={country}
            onChange={(e) => setCountry(e.target.value)}
          />
        </div>
      )}
      <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
        {paid.map((p) => {
          const isCurrent = p.slug === currentPlan;
          const usd = p.tenant_has_byok ? p.byok_price_usd : p.base_price_usd;
          const inr = p.tenant_has_byok ? p.byok_price_inr : p.base_price_inr;
          return (
            <div key={p.slug} className="tile" style={{ minWidth: 220, display: "grid", gap: 8 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <strong>{p.name}</strong>
                {isCurrent && <Tag tone="accent">current plan</Tag>}
              </div>
              {p.checkout_available ? (
                <div>
                  <div style={{ font: "600 22px/1 var(--font-heading)" }}>
                    ${(usd / 100).toFixed(2)}
                    <span className="muted" style={{ fontSize: 12 }}>
                      /mo
                    </span>
                  </div>
                  <div className="muted" style={{ fontSize: 11 }}>
                    ≈ ₹{(inr / 100).toLocaleString()}/mo
                  </div>
                </div>
              ) : (
                <div className="muted" style={{ fontSize: 15 }}>
                  Talk to us
                </div>
              )}
              <div className="muted" style={{ fontSize: 12 }}>
                {p.included_runs != null ? `${p.included_runs.toLocaleString()} runs/mo` : "custom volume"}
                {p.seats_included != null ? ` · ${p.seats_included} seats` : " · unlimited seats"}
              </div>
              {p.tenant_has_byok && p.byok_discount_pct > 0 && (
                <div style={{ fontSize: 11, color: "var(--accent)" }}>
                  {p.byok_discount_pct}% off — using your own LLM key
                </div>
              )}
              {p.checkout_available && !isCurrent && (
                <Button variant="primary" size="sm" disabled={busy === p.slug} onClick={() => subscribe(p.slug)}>
                  {busy === p.slug ? "Redirecting…" : `Upgrade to ${p.name}`}
                </Button>
              )}
            </div>
          );
        })}
      </div>
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

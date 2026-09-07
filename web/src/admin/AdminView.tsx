import { useEffect, useState } from "react";
import { Toolbar, Segmented } from "../ui";
import { BillingView } from "../billing/BillingView";
import { TeamView } from "../team/TeamView";
import { ActivityView } from "../activity/ActivityView";

type AdminTab = "billing" | "team" | "activity";

/**
 * One admin frame for the three thin views (Screen 06): a segmented switch
 * over one 24px content body. No view sets its own max-width.
 */
export function AdminView({ tenantId, tab: initialTab }: { tenantId: string; tab: AdminTab }) {
  const [tab, setTab] = useState<AdminTab>(initialTab);
  useEffect(() => setTab(initialTab), [initialTab]);

  return (
    <div className="admin-shell">
      <div className="app-toolbar">
        <Toolbar title="Admin">
          <Segmented
            options={[
              { value: "billing", label: "Billing" },
              { value: "team", label: "Team" },
              { value: "activity", label: "Activity" },
            ]}
            value={tab}
            onChange={setTab}
            aria-label="Admin section"
          />
        </Toolbar>
      </div>
      <div className="admin-body">
        {tab === "billing" ? (
          <BillingView tenantId={tenantId} />
        ) : tab === "team" ? (
          <TeamView tenantId={tenantId} />
        ) : (
          <ActivityView tenantId={tenantId} />
        )}
      </div>
    </div>
  );
}

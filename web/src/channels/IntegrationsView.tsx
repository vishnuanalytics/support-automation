import { useState } from "react";
import { Toolbar, Segmented } from "../ui";
import { ConnectionsView } from "./ConnectionsView";
import { ChannelsView } from "./ChannelsView";

/**
 * One shell for the two admin surfaces (Screen 05): a segmented switch
 * between Integrations (HTTP connections, Salesforce, Zendesk, AI models,
 * case taxonomy) and Channels (inbound email / chat adapters). The body is
 * the single scroll container.
 */
export function IntegrationsView({ tenantId }: { tenantId: string }) {
  const [tab, setTab] = useState<"integrations" | "channels">("integrations");
  return (
    <div className="int-shell">
      <div className="app-toolbar">
        <Toolbar title="Connections">
          <Segmented
            options={[
              { value: "integrations", label: "Integrations" },
              { value: "channels", label: "Channels" },
            ]}
            value={tab}
            onChange={setTab}
            aria-label="Section"
          />
        </Toolbar>
      </div>
      <div className="int-body">
        {tab === "integrations" ? (
          <ConnectionsView tenantId={tenantId} />
        ) : (
          <ChannelsView tenantId={tenantId} />
        )}
      </div>
    </div>
  );
}

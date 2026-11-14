import { Tag } from "../ui";
import { formatAge } from "./util";
import type { LastResponse as Latest } from "./types";

function partyText(party: string | undefined, thirdParty: string, customer: string) {
  if (party === "us") return "Agent";
  if (party === "engineering") return "Engineering";
  return party === "customer" ? customer : thirdParty;
}

/** Who spoke last on a case, where, and how long ago. */
export function LastResponse({ latest, thirdParty, customer, brief }: {
  latest: Latest; thirdParty: string; customer: string; brief?: boolean;
}) {
  if (!latest) return <span className="monitor-muted">No response found</span>;
  const hours = (Date.now() - new Date(latest.at).getTime()) / 3600000;
  return (
    <div className="last-response">
      <Tag tone={latest.party === "us" ? "neutral" : "warn-soft"}>{partyText(latest.party, thirdParty, customer)}</Tag>{" "}
      {latest.who} · {latest.source} · {formatAge(hours)} ago
      {!brief && latest.text && <p>{latest.text.slice(0, 220)}</p>}
    </div>
  );
}

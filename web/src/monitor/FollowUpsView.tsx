import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, EmptyState, Skeleton, Tag, type TagTone } from "../ui";
import { copyText } from "./util";
import type { FollowUpCase, FollowUpOwner, FollowUps } from "./types";

const ACTION: Record<FollowUpCase["action"], { label: string; tone: TagTone }> = {
  reply: { label: "Reply", tone: "warn" },
  raise: { label: "Raise", tone: "warn" },
  chase: { label: "Chase", tone: "neutral" },
};

function Owner({ owner, onOpen, onCopied }: {
  owner: FollowUpOwner;
  onOpen: (id: string) => void;
  onCopied: (name: string) => void;
}) {
  const { counts } = owner;
  const parts = [[counts.reply, "to reply"], [counts.raise, "to raise"], [counts.chase, "to chase"]]
    .filter(([n]) => n).map(([n, label]) => `${n} ${label}`);
  return (
    <details className="monitor-card" open={owner.mine}>
      <summary>
        {owner.owner} {owner.mine && <Tag>Mine</Tag>}{" "}
        <span className="monitor-fu-counts">
          {parts.join(" · ")}{owner.overdue ? ` · ${owner.overdue} overdue` : ""}
        </span>
      </summary>
      <Button size="sm" onClick={async () => { await copyText(owner.message); onCopied(owner.owner); }}>
        Copy message to {owner.owner.split(" ")[0]}
      </Button>
      <ol className="monitor-fu-list">
        {owner.cases.map((c) => (
          <li key={c.id}>
            <Tag tone={ACTION[c.action].tone}>{ACTION[c.action].label}</Tag>
            <button type="button" className="monitor-link" onClick={() => onOpen(c.id)}>
              {c.case_number}
            </button>
            <div>
              <div>{c.next_action}</div>
              <div className="monitor-fu-subject">
                {[c.third_parties[0], c.record_type, c.subject].filter(Boolean).join(" · ")}
              </div>
            </div>
          </li>
        ))}
      </ol>
    </details>
  );
}

export function FollowUpsView({ tenantId, onOpen }: { tenantId?: string; onOpen: (id: string) => void }) {
  const [data, setData] = useState<FollowUps | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    api.monitor.followUps(tenantId).then(setData).catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  if (err) return <Banner tone="exception" title={err} />;
  if (!data) return <Skeleton variant="row" lines={6} />;
  if (!data.owners.length) {
    return <EmptyState title="Nothing to follow up today" body="New work appears here after each sync." />;
  }
  return (
    <>
      <div className="monitor-fu-head">
        <div><strong>{data.owners.length}</strong>Agents to follow up</div>
        <div><strong>{data.counts.reply}</strong>Cases to reply to</div>
        <div><strong>{data.counts.raise}</strong>Cases to raise</div>
        <div><strong>{data.counts.chase}</strong>Quiet cases to chase</div>
      </div>
      {note && <p className="monitor-note" role="status">{note}</p>}
      {data.owners.map((o) => (
        <Owner key={o.owner} owner={o} onOpen={onOpen} onCopied={(n) => setNote(`Copied the message to ${n}`)} />
      ))}
    </>
  );
}

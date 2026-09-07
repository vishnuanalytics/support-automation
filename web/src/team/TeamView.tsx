import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { Invitation, Member } from "../types";
import { Button, Tag, Banner, Dialog, Field, Input, Select, ConfirmButton } from "../ui";

export function TeamView({ tenantId }: { tenantId: string }) {
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invitation[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"editor" | "viewer">("viewer");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);

  function load() {
    api.team.members(tenantId).then(setMembers).catch((e: ApiError) => setErr(e.message));
    api.team.invitations().then(setInvites).catch(() => {});
  }
  useEffect(load, [tenantId]);

  async function invite() {
    if (!email.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await api.team.invite({ email: email.trim().toLowerCase(), role, tenant_id: tenantId });
      setEmail("");
      setInviteOpen(false);
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setBusy(false);
  }

  const pending = invites.filter((i) => i.status === "pending");

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <p className="muted" style={{ fontSize: 12, margin: 0, maxWidth: "60ch" }}>
          An invite pre-authorises an email + role. The person gets access the next time they sign
          in — no email is sent.
        </p>
        <Button variant="primary" size="sm" onClick={() => setInviteOpen(true)}>
          Invite
        </Button>
      </div>

      {err && (
        <Banner
          tone="exception"
          title={err}
          actions={<Button variant="ghost" size="sm" onClick={() => setErr(null)}>Dismiss</Button>}
        />
      )}

      <div>
        <h5>Members</h5>
        <table className="runs-table">
          <thead>
            <tr>
              <th>email</th>
              <th>role</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.user_id}>
                <td>
                  {m.email || m.user_id} {m.is_you && <span className="muted">(you)</span>}
                </td>
                <td>
                  <Tag tone="neutral">{m.role}</Tag>
                </td>
                <td style={{ textAlign: "right" }}>
                  {!m.is_you && m.role !== "owner" && (
                    <ConfirmButton
                      label="Remove"
                      size="sm"
                      title={`Remove ${m.email || "this member"}?`}
                      confirmLabel="Remove"
                      onConfirm={() =>
                        api.team
                          .removeMember(m.user_id, tenantId)
                          .then(load)
                          .catch((e) => setErr(String(e)))
                      }
                    />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {pending.length > 0 && (
        <div>
          <h5>Pending invites</h5>
          <table className="runs-table">
            <thead>
              <tr>
                <th>email</th>
                <th>role</th>
                <th>sent</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {pending.map((i) => (
                <tr key={i.invite_id}>
                  <td>{i.email}</td>
                  <td>
                    <Tag tone="neutral">{i.role}</Tag>
                  </td>
                  <td className="muted">{new Date(i.created_at).toLocaleDateString()}</td>
                  <td style={{ textAlign: "right" }}>
                    <ConfirmButton
                      label="Revoke"
                      size="sm"
                      title={`Revoke the invite for ${i.email}?`}
                      confirmLabel="Revoke"
                      onConfirm={() =>
                        api.team.revoke(i.invite_id).then(load).catch((e) => setErr(String(e)))
                      }
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Dialog
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        title="Invite a teammate"
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setInviteOpen(false) },
          { label: busy ? "Inviting…" : "Send invite", variant: "primary", onClick: invite },
        ]}
      >
        <div style={{ display: "grid", gap: 12 }}>
          <Field label="Email">
            <Input
              type="email"
              autoFocus
              placeholder="teammate@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <Field label="Role">
            <Select
              value={role}
              onChange={(e) => setRole(e.target.value as "editor" | "viewer")}
              options={[
                { value: "viewer", label: "Can view" },
                { value: "editor", label: "Can edit" },
              ]}
            />
          </Field>
        </div>
      </Dialog>
    </div>
  );
}

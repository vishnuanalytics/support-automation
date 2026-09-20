import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { Invitation, Member } from "../types";
import { Button, Tag, Banner, Dialog, Field, Input, Select, ConfirmButton, Skeleton, StatTile } from "../ui";

const STATUS_TONE: Record<Invitation["status"], "success" | "accent" | "neutral"> = {
  accepted: "success",
  pending: "accent",
  revoked: "neutral",
};

/** The most recent status-changing event for an invite -- accepted_at or
 * revoked_at for the "Invite history" table, archived_at for the archived
 * one (a row only ever has at most one of these set at a time). null for
 * a still-pending invite, which hasn't been updated since it was created. */
function lastUpdatedAt(i: Invitation): string | null {
  return i.archived_at || i.accepted_at || i.revoked_at || null;
}

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString() : "—";
}

export function TeamView({ tenantId }: { tenantId: string }) {
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invitation[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"editor" | "viewer">("viewer");
  const [err, setErr] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "accent" | "warn"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [resendingId, setResendingId] = useState<string | null>(null);
  const [archivingId, setArchivingId] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);

  /** already_registered means Supabase declined to send a *new-user*
   * invite because this email already has an account -- not a failure:
   * that person already has credentials and gets access automatically
   * the moment they sign in (accept_invitations runs on every sign-in). */
  function emailFailureNotice(
    inviteEmail: string,
    res: { email_error: string | null; already_registered: boolean },
  ): { tone: "accent" | "warn"; text: string } {
    if (res.already_registered) {
      return {
        tone: "accent",
        text: `${inviteEmail} already has an account — no email needed. They'll get access to this workspace automatically the next time they sign in.`,
      };
    }
    return {
      tone: "warn",
      text:
        `The invite for ${inviteEmail} was created, but the email failed to send` +
        (res.email_error ? `: ${res.email_error}` : "") +
        ". They can still get in by signing up directly with that email address.",
    };
  }

  function load() {
    api.team.members(tenantId).then(setMembers).catch((e: ApiError) => setErr(e.message)).finally(() => setLoading(false));
    api.team.invitations().then(setInvites).catch(() => {});
  }
  useEffect(() => {
    setLoading(true);
    load();
  }, [tenantId]);

  async function invite() {
    if (!email.trim()) return;
    setBusy(true);
    setErr(null);
    setNotice(null);
    try {
      const res = await api.team.invite({ email: email.trim().toLowerCase(), role, tenant_id: tenantId });
      if (!res.email_sent) setNotice(emailFailureNotice(res.email, res));
      setEmail("");
      setInviteOpen(false);
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setBusy(false);
  }

  async function resend(inviteId: string, inviteEmail: string) {
    setResendingId(inviteId);
    setErr(null);
    setNotice(null);
    try {
      const res = await api.team.resend(inviteId);
      setNotice(
        res.email_sent
          ? { tone: "success", text: `Resent the invite to ${inviteEmail}.` }
          : emailFailureNotice(inviteEmail, res),
      );
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setResendingId(null);
  }

  async function archive(inviteId: string) {
    setArchivingId(inviteId);
    setErr(null);
    try {
      await api.team.archive(inviteId);
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setArchivingId(null);
  }

  const pending = invites.filter((i) => i.status === "pending");
  // "history" = every invite ever sent, minus the still-open pending ones --
  // this is what tells an owner exactly how many people they've invited and
  // whether each one is accepted or was revoked, not just what's currently
  // outstanding.
  const history = invites.filter((i) => i.status !== "pending" && !i.archived_at);
  const archived = invites.filter((i) => i.status !== "pending" && i.archived_at);
  const acceptedCount = invites.filter((i) => i.status === "accepted").length;
  const revokedCount = invites.filter((i) => i.status === "revoked").length;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <p className="muted" style={{ fontSize: 12, margin: 0, maxWidth: "60ch" }}>
          An invite pre-authorises an email + role for <strong>this workspace only</strong> — it
          doesn't grant access to any other workspace, even one you also belong to. We email them a
          sign-in link; they also get access automatically if they sign in with that email another way.
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
      {notice && (
        <Banner
          tone={notice.tone}
          title={notice.text}
          actions={<Button variant="ghost" size="sm" onClick={() => setNotice(null)}>Dismiss</Button>}
        />
      )}

      {!loading && invites.length > 0 && (
        <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
          <StatTile label="invites sent" value={invites.length} />
          <StatTile label="pending" value={pending.length} tone="accent" />
          <StatTile label="accepted" value={acceptedCount} tone="success" />
          <StatTile label="revoked" value={revokedCount} />
        </div>
      )}

      <div>
        <h5>Members</h5>
        {loading ? (
          <Skeleton variant="row" lines={3} />
        ) : (
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
        )}
      </div>

      {!loading && pending.length > 0 && (
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
                  <td className="muted">{fmtDate(i.created_at)}</td>
                  <td style={{ textAlign: "right" }}>
                    <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={resendingId === i.invite_id}
                        onClick={() => resend(i.invite_id, i.email)}
                      >
                        {resendingId === i.invite_id ? "Resending…" : "Resend"}
                      </Button>
                      <ConfirmButton
                        label="Revoke"
                        size="sm"
                        title={`Revoke the invite for ${i.email}?`}
                        confirmLabel="Revoke"
                        onConfirm={() =>
                          api.team.revoke(i.invite_id).then(load).catch((e) => setErr(String(e)))
                        }
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!loading && history.length > 0 && (
        <div>
          <h5>Invite history</h5>
          <table className="runs-table">
            <thead>
              <tr>
                <th>email</th>
                <th>role</th>
                <th>status</th>
                <th>created</th>
                <th>last updated</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {history.map((i) => (
                <tr key={i.invite_id}>
                  <td>{i.email}</td>
                  <td>
                    <Tag tone="neutral">{i.role}</Tag>
                  </td>
                  <td>
                    <Tag tone={STATUS_TONE[i.status]}>{i.status}</Tag>
                  </td>
                  <td className="muted">{fmtDate(i.created_at)}</td>
                  <td className="muted">{fmtDate(lastUpdatedAt(i))}</td>
                  <td style={{ textAlign: "right" }}>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={archivingId === i.invite_id}
                      onClick={() => archive(i.invite_id)}
                    >
                      {archivingId === i.invite_id ? "Archiving…" : "Archive"}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!loading && archived.length > 0 && (
        <div>
          <Button variant="ghost" size="sm" onClick={() => setShowArchived((s) => !s)}>
            {showArchived ? "Hide" : "Show"} archived ({archived.length})
          </Button>
          {showArchived && (
            <table className="runs-table" style={{ marginTop: 8 }}>
              <thead>
                <tr>
                  <th>email</th>
                  <th>role</th>
                  <th>status</th>
                  <th>created</th>
                  <th>last updated</th>
                </tr>
              </thead>
              <tbody>
                {archived.map((i) => (
                  <tr key={i.invite_id}>
                    <td className="muted">{i.email}</td>
                    <td>
                      <Tag tone="neutral">{i.role}</Tag>
                    </td>
                    <td>
                      <Tag tone={STATUS_TONE[i.status]}>{i.status}</Tag>
                    </td>
                    <td className="muted">{fmtDate(i.created_at)}</td>
                    <td className="muted">{fmtDate(lastUpdatedAt(i))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
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

import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { Invitation, Member } from "../types";

export function TeamView() {
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invitation[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"editor" | "viewer">("viewer");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function load() {
    api.team.members().then(setMembers).catch((e: ApiError) => setErr(e.message));
    api.team.invitations().then(setInvites).catch(() => {});
  }
  useEffect(load, []);

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.team.invite({ email: email.trim().toLowerCase(), role });
      setEmail("");
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setBusy(false);
  }

  const pending = invites.filter((i) => i.status === "pending");

  return (
    <div className="pane" style={{ overflow: "auto", padding: 16, maxWidth: 720 }}>
      <h4>Team</h4>
      <p className="muted" style={{ fontSize: 12 }}>
        An invite pre-authorises an email + role. The person gets access the next
        time they sign in — no email is sent.
      </p>

      <form className="row" onSubmit={invite} style={{ gap: 6, margin: "10px 0" }}>
        <input
          type="email"
          placeholder="teammate@company.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          style={{ maxWidth: 260 }}
        />
        <select value={role} onChange={(e) => setRole(e.target.value as "editor" | "viewer")}
          style={{ width: "auto" }}>
          <option value="viewer">can view</option>
          <option value="editor">can edit</option>
        </select>
        <button className="primary" type="submit" disabled={busy || !email}>Invite</button>
      </form>
      {err && <div className="banner err">{err}</div>}

      <h5>Members</h5>
      <table className="runs-table">
        <thead><tr><th>email</th><th>role</th><th></th></tr></thead>
        <tbody>
          {members.map((m) => (
            <tr key={m.user_id}>
              <td>{m.email || m.user_id} {m.is_you && <span className="muted">(you)</span>}</td>
              <td><span className="pill">{m.role}</span></td>
              <td style={{ textAlign: "right" }}>
                {!m.is_you && m.role !== "owner" && (
                  <button className="err" onClick={() =>
                    api.team.removeMember(m.user_id).then(load).catch((e) => setErr(String(e)))}>
                    remove
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {pending.length > 0 && (
        <>
          <h5>Pending invites</h5>
          <table className="runs-table">
            <thead><tr><th>email</th><th>role</th><th>sent</th><th></th></tr></thead>
            <tbody>
              {pending.map((i) => (
                <tr key={i.invite_id}>
                  <td>{i.email}</td>
                  <td><span className="pill">{i.role}</span></td>
                  <td className="muted">{new Date(i.created_at).toLocaleDateString()}</td>
                  <td style={{ textAlign: "right" }}>
                    <button className="err" onClick={() =>
                      api.team.revoke(i.invite_id).then(load).catch((e) => setErr(String(e)))}>
                      revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

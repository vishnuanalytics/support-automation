import { useState } from "react";
import { supabase } from "../supabase";
import { Button, Field, Input } from "../ui";

export function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function withPassword(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) setMsg(error.message);
    setBusy(false);
  }

  async function magicLink() {
    setBusy(true);
    setMsg(null);
    const { error } = await supabase.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.origin },
    });
    setMsg(error ? error.message : "Check your email for the sign-in link.");
    setBusy(false);
  }

  async function withGoogle() {
    setBusy(true);
    setMsg(null);
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: window.location.origin },
    });
    if (error) {
      setMsg(error.message);
      setBusy(false);
    }
  }

  return (
    <div className="login-split">
      <div className="login-split__form">
        <span
          style={{
            font: "var(--type-kicker)",
            letterSpacing: "0.16em",
            textTransform: "uppercase",
            color: "var(--accent)",
          }}
        >
          Support automation
        </span>
        <div style={{ font: "var(--type-view-title)" }}>Sign in</div>

        <Button variant="secondary" onClick={withGoogle} disabled={busy}>
          Continue with Google
        </Button>

        <div className="row" style={{ gap: 8, alignItems: "center", margin: "4px 0" }}>
          <hr style={{ flex: 1, border: 0, borderTop: "1px solid var(--line)" }} />
          <span className="muted" style={{ fontSize: 11 }}>or email</span>
          <hr style={{ flex: 1, border: 0, borderTop: "1px solid var(--line)" }} />
        </div>

        <form onSubmit={withPassword} style={{ display: "grid", gap: 10 }}>
          <Field label="Email">
            <Input
              type="email"
              placeholder="you@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </Field>
          <Field label="Password" hint="or use the magic link">
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <div className="row" style={{ gap: 8 }}>
            <Button variant="primary" type="submit" disabled={busy || !email}>
              Continue
            </Button>
            <Button variant="ghost" type="button" onClick={magicLink} disabled={busy || !email}>
              Email me a link
            </Button>
          </div>
        </form>

        {msg && <div className="muted" style={{ fontSize: 12 }}>{msg}</div>}
      </div>

      <div className="login-split__aside">
        <div className="login-bars" aria-hidden>
          <span style={{ background: "var(--accent-pressed)", height: "60%" }} />
          <span style={{ background: "var(--accent)", height: "100%" }} />
          <span style={{ background: "var(--exception)", height: "44%" }} />
          <span style={{ background: "var(--warn)", height: "72%" }} />
        </div>
        <div style={{ font: "600 15px/1.35 var(--font-body)" }}>The agent is data, not code.</div>
        <div className="muted" style={{ font: "400 12.5px/1.6 var(--font-body)" }}>
          Node types, edges and config live in Postgres; this editor is a second client on the
          same schema.
        </div>
      </div>
    </div>
  );
}

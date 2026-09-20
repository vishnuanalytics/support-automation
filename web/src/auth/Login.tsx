import { useEffect, useState } from "react";
import { supabase } from "../supabase";
import { Button, Field, Input } from "../ui";

// Magic-link and password-reset both send a real email through the same
// project-wide Supabase Auth quota that a rapid-click loop can exhaust in
// minutes (this is exactly what happened to real invite emails elsewhere
// in this app) -- a short client-side cooldown after either one stops that
// class of accidental misuse. It's a courtesy, not the real rate limit:
// Supabase's own Auth -> Rate Limits enforces the authoritative cap
// server-side regardless of what this does.
const EMAIL_COOLDOWN_SECONDS = 30;

export function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [emailCooldownUntil, setEmailCooldownUntil] = useState(0);
  const [, tick] = useState(0);

  useEffect(() => {
    if (!emailCooldownUntil) return;
    const id = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [emailCooldownUntil]);

  const cooldownSecondsLeft = Math.max(0, Math.ceil((emailCooldownUntil - Date.now()) / 1000));
  const emailOnCooldown = cooldownSecondsLeft > 0;

  async function withPassword(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) setMsg(error.message);
    setBusy(false);
  }

  async function forgotPassword() {
    if (!email) {
      setMsg("Enter your email above first.");
      return;
    }
    setBusy(true);
    setMsg(null);
    setEmailCooldownUntil(Date.now() + EMAIL_COOLDOWN_SECONDS * 1000);
    const { error } = await supabase.auth.resetPasswordForEmail(email, {
      redirectTo: window.location.origin,
    });
    setMsg(error ? error.message : "Check your email for a password reset link.");
    setBusy(false);
  }

  async function magicLink() {
    setBusy(true);
    setMsg(null);
    setEmailCooldownUntil(Date.now() + EMAIL_COOLDOWN_SECONDS * 1000);
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
            <Button variant="ghost" type="button" onClick={magicLink} disabled={busy || !email || emailOnCooldown}>
              {emailOnCooldown ? `Wait ${cooldownSecondsLeft}s…` : "Email me a link"}
            </Button>
          </div>
          <Button
            variant="ghost"
            size="sm"
            type="button"
            onClick={forgotPassword}
            disabled={busy || !email || emailOnCooldown}
            style={{ justifySelf: "start" }}
          >
            {emailOnCooldown ? `Forgot password? (wait ${cooldownSecondsLeft}s)` : "Forgot password?"}
          </Button>
        </form>

        {msg && <div className="muted" style={{ fontSize: 12 }}>{msg}</div>}
      </div>

      <div className="login-split__aside">
        <div className="login-split__mark" aria-hidden>A</div>
        <div style={{ font: "600 20px/1.35 var(--font-heading)" }}>The agent is data, not code.</div>
        <div className="muted" style={{ font: "400 13.5px/1.6 var(--font-body)" }}>
          Node types, edges and config live in Postgres; this editor is a second client on the
          same schema.
        </div>
      </div>
    </div>
  );
}

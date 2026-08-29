import { useState } from "react";
import { supabase } from "../supabase";

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

  return (
    <div className="login col">
      <h1>Support flow editor</h1>
      <p className="muted">Sign in with your Supabase account.</p>
      <form className="col" onSubmit={withPassword}>
        <input
          type="email"
          placeholder="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <input
          type="password"
          placeholder="password (or use the magic link)"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <div className="row">
          <button className="primary" type="submit" disabled={busy || !email}>
            Sign in
          </button>
          <button type="button" onClick={magicLink} disabled={busy || !email}>
            Email me a link
          </button>
        </div>
      </form>
      {msg && <div className="muted">{msg}</div>}
    </div>
  );
}

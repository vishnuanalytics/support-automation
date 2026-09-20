import { useEffect, useState } from "react";
import { supabase } from "../supabase";
import { Button, Field, Input, Banner } from "../ui";

// A short blocklist of the passwords most likely to still satisfy the
// length/character-class rules below (e.g. "Password123!") but that offer
// no real protection -- catches the obvious case client-side. The
// authoritative check against actually-breached passwords belongs in
// Supabase's own "leaked password protection" (Auth -> Policies in the
// dashboard, HaveIBeenPwned-backed), not a hardcoded list here.
const COMMON_WEAK = new Set([
  "password", "password1", "password123", "12345678", "123456789",
  "qwerty123", "letmein123", "welcome123", "changeme123", "iloveyou1",
  "admin1234", "abc123456",
]);

function localPart(email: string | null | undefined): string {
  return (email || "").split("@")[0].toLowerCase();
}

function passwordRules(password: string, email: string | null | undefined) {
  const lp = localPart(email);
  return [
    { label: "At least 12 characters", met: password.length >= 12 },
    { label: "An uppercase and a lowercase letter", met: /[a-z]/.test(password) && /[A-Z]/.test(password) },
    { label: "A number", met: /[0-9]/.test(password) },
    { label: "A symbol (e.g. ! ? # -)", met: /[^A-Za-z0-9]/.test(password) },
    {
      label: "Not a commonly used password",
      met: password.length > 0 && !COMMON_WEAK.has(password.toLowerCase()),
    },
    {
      label: "Doesn't contain your email",
      met: password.length > 0 && (!lp || lp.length < 3 || !password.toLowerCase().includes(lp)),
    },
  ];
}

/** A new-password form shared by two callers that both already have an
 * authenticated session -- the account row's "Set password" dialog (for
 * someone who's only ever signed in via magic link or Google and has
 * never had one) and the password-reset recovery screen (App.tsx, on the
 * PASSWORD_RECOVERY auth event). Both just call the same authenticated
 * `updateUser` -- no separate email round-trip needed once a session
 * already exists.
 *
 * This client-side checklist is UX, not the security boundary -- it can be
 * bypassed by anyone calling the Supabase API directly. The real
 * enforcement has to live in the Supabase project's own Auth -> Policies
 * password-strength settings (minimum length + character requirements +
 * leaked-password protection), which this app has no API access to set;
 * these rules are deliberately at least as strict as what's recommended
 * there, so a password accepted here should never be rejected server-side. */
export function SetNewPassword({ onDone, submitLabel = "Save password" }: {
  onDone: () => void;
  submitLabel?: string;
}) {
  const [email, setEmail] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    supabase.auth.getUser().then(({ data }) => setEmail(data.user?.email ?? null));
  }, []);

  const rules = passwordRules(password, email);
  const allMet = rules.every((r) => r.met);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (!allMet) {
      setErr("That password doesn't meet the requirements below yet.");
      return;
    }
    if (password !== confirm) {
      setErr("Passwords don't match.");
      return;
    }
    setBusy(true);
    setErr(null);
    const { error } = await supabase.auth.updateUser({ password });
    setBusy(false);
    if (error) {
      setErr(error.message);
      return;
    }
    onDone();
  }

  return (
    <form onSubmit={submit} style={{ display: "grid", gap: 10 }}>
      <Field label="New password">
        <Input
          type="password"
          value={password}
          onChange={(e) => { setPassword(e.target.value); setTouched(true); }}
          autoFocus
          required
        />
      </Field>
      <ul style={{ margin: "-4px 0 0", padding: 0, listStyle: "none", display: "grid", gap: 3 }}>
        {rules.map((r) => (
          <li
            key={r.label}
            style={{
              fontSize: 12,
              display: "flex",
              alignItems: "center",
              gap: 6,
              color: !touched ? "var(--muted)" : r.met ? "var(--success)" : "var(--exception)",
            }}
          >
            <span aria-hidden>{!touched ? "•" : r.met ? "✓" : "✕"}</span>
            {r.label}
          </li>
        ))}
      </ul>
      <Field label="Confirm password">
        <Input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          required
        />
      </Field>
      {err && <Banner tone="exception" title={err} />}
      <Button variant="primary" type="submit" disabled={busy}>
        {busy ? "Saving…" : submitLabel}
      </Button>
    </form>
  );
}

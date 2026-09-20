import { useState } from "react";
import { supabase } from "../supabase";
import { Button, Field, Input, Banner } from "../ui";

/** A new-password form shared by two callers that both already have an
 * authenticated session -- the account row's "Set password" dialog (for
 * someone who's only ever signed in via magic link or Google and has
 * never had one) and the password-reset recovery screen (App.tsx, on the
 * PASSWORD_RECOVERY auth event). Both just call the same authenticated
 * `updateUser` -- no separate email round-trip needed once a session
 * already exists. */
export function SetNewPassword({ onDone, submitLabel = "Save password" }: {
  onDone: () => void;
  submitLabel?: string;
}) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password.length < 8) {
      setErr("Use at least 8 characters.");
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
          onChange={(e) => setPassword(e.target.value)}
          autoFocus
          required
        />
      </Field>
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

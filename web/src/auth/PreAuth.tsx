import { useState } from "react";
import { LandingPage } from "../landing/LandingPage";
import { PrivacyPolicy } from "../landing/PrivacyPolicy";
import { TermsOfService } from "../landing/TermsOfService";
import { PublicShell } from "../landing/PublicShell";
import type { PublicView } from "../landing/types";
import { Login } from "./Login";

/**
 * Everything shown before sign-in: a real landing page (what the product
 * is, why it's worth signing in for) plus the login form itself and the
 * legal pages, all sharing one header/footer via PublicShell. Previously
 * `App.tsx` rendered `<Login />` directly with nothing in front of it —
 * this replaces that with a proper front door.
 *
 * Deliberately plain React state, not URL-synced (no router in this app,
 * and the Supabase client's OAuth/magic-link redirect also lands on this
 * origin and may itself use a URL hash — this avoids fighting over it).
 */
export function PreAuth() {
  const [view, setView] = useState<PublicView>("landing");

  return (
    <PublicShell view={view} onNavigate={setView}>
      {view === "landing" && <LandingPage onSignIn={() => setView("login")} />}
      {view === "login" && <Login />}
      {view === "privacy" && <PrivacyPolicy />}
      {view === "terms" && <TermsOfService />}
    </PublicShell>
  );
}

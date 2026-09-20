import type { ReactNode } from "react";
import { Button, ThemeToggle } from "../ui";
import type { PublicView } from "./types";

const CONTACT_EMAIL = "gundamvishnu7@gmail.com";

/**
 * Shared header/footer for every pre-auth screen (landing, sign in, Privacy
 * Policy, Terms of Service) — one place for the brand mark, the Sign in
 * call-to-action, and the legal/contact links, so those don't get
 * duplicated (and drift) across four separate page components.
 */
export function PublicShell({
  view,
  onNavigate,
  children,
}: {
  view: PublicView;
  onNavigate: (v: PublicView) => void;
  children: ReactNode;
}) {
  return (
    <div className="public-shell">
      <header className="public-header">
        <button type="button" className="public-brand" onClick={() => onNavigate("landing")}>
          <span className="public-brand__mark" aria-hidden>A</span>
          <span className="public-brand__word">Support Automation</span>
        </button>
        <nav className="public-nav">
          {view !== "landing" && (
            <button type="button" className="public-link" onClick={() => onNavigate("landing")}>
              ← Home
            </button>
          )}
          {view !== "login" && (
            <Button variant="primary" size="sm" onClick={() => onNavigate("login")}>
              Sign in
            </Button>
          )}
          <ThemeToggle />
        </nav>
      </header>

      <main className="public-main">{children}</main>

      <footer className="public-footer">
        <div className="public-footer__links">
          <button type="button" className="public-link" onClick={() => onNavigate("privacy")}>
            Privacy Policy
          </button>
          <button type="button" className="public-link" onClick={() => onNavigate("terms")}>
            Terms of Service
          </button>
          <a className="public-link" href={`mailto:${CONTACT_EMAIL}`}>
            Contact — {CONTACT_EMAIL}
          </a>
        </div>
        <div className="muted public-footer__copy">
          © {new Date().getFullYear()} Support Automation. All rights reserved.
        </div>
      </footer>
    </div>
  );
}

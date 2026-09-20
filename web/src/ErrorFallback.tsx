import { Button } from "./ui";
import { sentryConfigured } from "./sentry";

/**
 * Wraps <App /> in main.tsx. Before this, there was no error boundary
 * anywhere in the app at all -- any uncaught render exception (three real
 * ones were found and fixed elsewhere this session: ReviewView,
 * ConnectionsView, BillingView, all from stale mock/response shapes) blanked
 * the whole page to white, with the actual error visible only in a browser
 * console no real user ever opens. This is the safety net underneath that:
 * still a crash, but a legible one with a way back, and (if VITE_SENTRY_DSN
 * is set) a note that it's already been reported.
 */
export function ErrorFallback() {
  return (
    <div
      style={{
        display: "grid", placeItems: "center", minHeight: "100vh", padding: 24,
        textAlign: "center", gap: 12,
      }}
    >
      <div style={{ display: "grid", gap: 8, maxWidth: 420 }}>
        <h1 style={{ font: "var(--type-view-title)", margin: 0 }}>Something went wrong</h1>
        <p className="muted" style={{ margin: 0 }}>
          This page hit an unexpected error.{" "}
          {sentryConfigured
            ? "It's already been reported and we'll look into it."
            : "Reloading usually fixes it."}
        </p>
        <div>
          <Button variant="primary" onClick={() => window.location.reload()}>
            Reload
          </Button>
        </div>
      </div>
    </div>
  );
}

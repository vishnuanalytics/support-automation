import { defineConfig, devices } from "@playwright/test";

/**
 * Phase 9 residual, closed 2026-09-05 — the flow editor had zero automated
 * browser coverage; every picker bug in this codebase's history (see
 * PROJECT_SCOPE.md's "First real browser click-through") was found by a
 * human clicking through manually. These specs run with every `/api/**`
 * call intercepted (see e2e/mocks.ts) and a fake Supabase session seeded
 * into localStorage before the app boots — no real Supabase project,
 * Salesforce org, or backend process is needed, so this suite runs in CI
 * with zero secrets, same "offline by default" posture as the pytest suite.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: "http://127.0.0.1:5174",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  // `--mode e2e` -> Vite loads .env.e2e (fixed fake Supabase project ref,
  // so the localStorage key the spec seeds — sb-e2e-fixture-auth-token —
  // is deterministic), NOT the real web/.env.local used for `npm run dev`.
  // import.meta.env.VITE_* is substituted at build/transform time, so this
  // can't be done via webServer.env (that only sets the *process* env of
  // the dev-server command, too late to affect an already-transformed
  // module) — a real gotcha, hit and fixed while building this suite.
  webServer: {
    // `--host 127.0.0.1` pins Vite's bind address to match `url` below
    // exactly. Without it Vite binds its default "localhost", which some
    // CI runners' network stacks resolve to IPv6 (`::1`) — the server is
    // actually up, but Playwright's health check keeps hitting the IPv4
    // loopback and times out waiting for a server that's really already
    // there. (Real failure, not theoretical: this is exactly what broke
    // CI here — passed 3/3 in this sandbox, then timed out on GitHub's
    // runner. `stdout`/`stderr: "pipe"` added at the same time so a future
    // webServer failure actually shows Vite's own boot log in CI instead
    // of nothing.)
    command: "npm run dev -- --mode e2e --host 127.0.0.1 --port 5174 --strictPort",
    url: "http://127.0.0.1:5174",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});

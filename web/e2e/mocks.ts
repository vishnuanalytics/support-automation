import type { Page } from "@playwright/test";

/**
 * Every network call this suite needs, faked at the browser's network layer
 * (page.route) — no real Supabase project, Salesforce org, or backend
 * process required. `playwright.config.ts` points VITE_SUPABASE_URL at a
 * fixed fake project ref so the localStorage key this seeds
 * (`sb-e2e-fixture-auth-token`) is deterministic.
 */

export const FAKE_TENANT_ID = "11111111-1111-1111-1111-111111111111";
export const FAKE_FLOW_ID = "22222222-2222-2222-2222-222222222222";
const FAKE_USER_ID = "99999999-9999-9999-9999-999999999999";

export async function seedFakeSession(page: Page): Promise<void> {
  await page.addInitScript(
    ({ tenantId, userId }) => {
      const now = new Date().toISOString();
      const session = {
        access_token: "e2e-fake-access-token",
        token_type: "bearer",
        expires_in: 3600,
        expires_at: Math.floor(Date.now() / 1000) + 3600 * 24 * 365,
        refresh_token: "e2e-fake-refresh-token",
        user: {
          id: userId,
          aud: "authenticated",
          role: "authenticated",
          email: "e2e@test.local",
          app_metadata: {},
          user_metadata: {},
          identities: [],
          created_at: now,
          updated_at: now,
        },
      };
      window.localStorage.setItem("sb-e2e-fixture-auth-token", JSON.stringify(session));
      // skip the one-time onboarding-wizard redirect (App.tsx) — this suite
      // is testing the flow editor, not the onboarding flow
      window.localStorage.setItem(`onboarding-dismissed:${tenantId}`, "1");
    },
    { tenantId: FAKE_TENANT_ID, userId: FAKE_USER_ID },
  );
}

function fakeFlow(overrides: Record<string, unknown> = {}) {
  return {
    flow_id: FAKE_FLOW_ID,
    tenant_id: FAKE_TENANT_ID,
    team: "support",
    name: "E2E Test Flow",
    status: "draft",
    version: 1,
    published_version: null,
    sf_entry: false,
    nodes: [
      { node_id: "n1", type: "retrieve", label: null, position_x: 100, position_y: 100, config: {} },
    ],
    edges: [],
    ...overrides,
  };
}

export async function installApiMocks(
  page: Page,
  opts: { onSave?: (body: Record<string, unknown>) => void } = {},
): Promise<void> {
  let flow = fakeFlow();

  await page.route("**/api/invitations/accept", (route) =>
    route.fulfill({ json: { accepted: 0 } }));

  await page.route("**/api/tenants", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({
      json: [{ tenant_id: FAKE_TENANT_ID, role: "owner", name: "E2E Workspace" }],
    });
  });

  await page.route("**/api/templates", (route) => route.fulfill({ json: [] }));

  await page.route(/\/api\/models(\?.*)?$/, (route) => route.fulfill({ json: { models: [] } }));

  await page.route("**/api/node-types", (route) =>
    route.fulfill({
      json: {
        types: ["retrieve", "classify", "draft", "confidence_gate", "auto_reply", "ask_human"],
        defaults: {
          retrieve: {}, classify: {}, draft: {}, confidence_gate: {}, auto_reply: {}, ask_human: {},
        },
      },
    }));

  await page.route(`**/api/flows/${FAKE_FLOW_ID}/versions`, (route) =>
    route.fulfill({ json: [{ version: 1, created_at: new Date().toISOString() }] }));

  await page.route(`**/api/flows/${FAKE_FLOW_ID}`, (route) => {
    const method = route.request().method();
    if (method === "GET") return route.fulfill({ json: flow });
    if (method === "PUT") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      opts.onSave?.(body);
      flow = { ...flow, ...body, version: (flow.version as number) + 1 };
      return route.fulfill({ json: flow });
    }
    return route.fallback();
  });

  await page.route("**/api/flows", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({ json: [flow] });
  });
}

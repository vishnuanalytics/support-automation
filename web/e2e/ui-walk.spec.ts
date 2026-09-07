import { test, expect, type Page } from "@playwright/test";
import { seedFakeSession, FAKE_TENANT_ID, FAKE_FLOW_ID } from "./mocks";

/**
 * A whole-UI smoke walk. Every left-nav view is opened in turn; the test
 * fails if any of them throws an uncaught error, logs a console error, or
 * renders nothing. Plus a regression guard for the tab-switch bug
 * (auth-js re-emits SIGNED_IN on visibilitychange -> the app used to
 * refetch tenants and unmount the shell back to the default view).
 *
 * All network is faked (see installWalkMocks). No backend needed.
 */

const NOW = new Date().toISOString();

function jsonFor(url: string): unknown {
  const path = new URL(url).pathname.replace(/^\/api/, "");

  // ---- exact-ish matches first ----
  if (path === "/tenants")
    return [{ tenant_id: FAKE_TENANT_ID, role: "owner", name: "Walk Workspace" }];
  if (path === "/node-types")
    return { types: ["retrieve", "classify", "draft"], defaults: { retrieve: {}, classify: {}, draft: {} } };
  if (path === "/templates") return [];
  if (path.startsWith("/models")) return { models: [] };
  if (path === "/flows")
    return [
      {
        flow_id: FAKE_FLOW_ID,
        tenant_id: FAKE_TENANT_ID,
        team: "support",
        name: "Walk Flow",
        status: "draft",
        version: 1,
        published_version: null,
        sf_entry: false,
      },
    ];
  if (path === `/flows/${FAKE_FLOW_ID}`)
    return {
      flow_id: FAKE_FLOW_ID,
      tenant_id: FAKE_TENANT_ID,
      team: "support",
      name: "Walk Flow",
      status: "draft",
      version: 1,
      published_version: null,
      sf_entry: false,
      nodes: [{ node_id: "n1", type: "retrieve", label: null, position_x: 80, position_y: 80, config: {} }],
      edges: [],
    };
  if (path === `/flows/${FAKE_FLOW_ID}/versions`) return [{ version: 1, created_at: NOW }];
  if (path.endsWith("/triggers")) return [];

  if (path === "/runs/stats")
    return {
      total: 0,
      by_outcome: {},
      by_tier: {},
      low_confidence: 0,
      by_human_action: {},
      draft_acceptance: null,
    };
  if (path.startsWith("/runs")) return [];
  if (path.startsWith("/audit")) return [];
  if (path.startsWith("/review-tasks")) return [];
  if (path.startsWith("/action-requests")) return [];
  if (path === "/approvals") return { review_tasks: [], action_requests: [] };
  if (path.startsWith("/kil/metrics"))
    return {
      window_days: 30,
      review: {
        total: 0,
        open: 0,
        by_trigger: {},
        by_status: {},
        resolved: 0,
        flag_precision: null,
        false_flag_rate: null,
        agent_correction_rate: null,
        median_time_to_review_h: null,
      },
      kb_writeback: { entries: 0, provisional: 0, active: 0, superseded: 0, promotion_rate: null },
      knowledge_freshness_days: null,
      by_source: [],
      weekly: [],
    };
  if (path === "/health/tenant")
    return {
      tenant_id: FAKE_TENANT_ID,
      connections: { failing: 0, sample: null },
      review_backlog: { open: 0, oldest_days: null },
      reasoning_stuck: 0,
      doc_writebacks_pending: 0,
      failed_jobs_24h: 0,
      runs_24h: { total: 0, by_outcome: {}, struggle_rate: 0 },
      system_stale: [],
    };

  if (path === "/kb/collections")
    return [
      {
        source_id: "org-kb",
        name: "Organization knowledge",
        description: null,
        tenant_id: FAKE_TENANT_ID,
        entry_count: 0,
        org_kb: true,
      },
    ];
  if (/^\/kb\/collections\/[^/]+\/entries$/.test(path)) return [];
  if (/^\/kb\/collections\/[^/]+\/connections$/.test(path)) return [];
  if (/^\/kb\/collections\/[^/]+\/doc-writebacks$/.test(path)) return [];
  if (path.startsWith("/kb/connectors")) return [];
  if (path.startsWith("/kb/doc-writebacks")) return [];
  if (path.startsWith("/kb/doc-defaults"))
    return { tenant_id: FAKE_TENANT_ID, stored: {}, effective: {} };

  if (path.endsWith("/status") || path.includes("/status?"))
    return { configured: false, connected: {} };
  if (path === "/integrations/salesforce/oauth/status") return { configured: false };
  if (path.startsWith("/integrations/salesforce")) return [];
  if (path.startsWith("/integrations/llm"))
    return {
      tenant_id: FAKE_TENANT_ID,
      tenant: { groq: false, anthropic: false, openrouter: false },
      platform: { groq: true, anthropic: false, openrouter: false },
    };
  if (path.startsWith("/integrations/zendesk"))
    return { tenant_id: FAKE_TENANT_ID, configured: false, status: "none" };
  if (path.startsWith("/salesforce/meta")) return { queues: [], record_types: [], case_fields: [] };
  if (path.startsWith("/slack/meta")) return { channels: [], users: [] };
  if (path.startsWith("/integrations/email"))
    return { configured: false, connected: false };
  if (path.startsWith("/integrations/freshchat"))
    return { configured: false, connected: false };
  if (path.startsWith("/integrations/posthog"))
    return { configured: false, connected: false };

  if (path.startsWith("/tenants/case-connector"))
    return { tenant_id: FAKE_TENANT_ID, case_connector: "salesforce" };
  if (path.startsWith("/tenants/case-taxonomy"))
    return { tenant_id: FAKE_TENANT_ID, config: {}, updated_at: null, defaults: {} };

  if (/^\/connections\/[^/]+\/actions/.test(path)) return [];
  if (path.startsWith("/connections"))
    // one row so the table body (a keyed Fragment per connection) renders
    return [{ slug: "vendor-api", base_url: "https://api.vendor.com", auth: { type: "bearer" }, has_secret: true }];
  if (path.startsWith("/connectors")) return [];
  if (path.startsWith("/members")) return [];
  if (path === "/invitations") return [];

  if (path.startsWith("/billing/usage"))
    return {
      period_label: "2026-09",
      period: { start: NOW, end: NOW },
      plan: "free",
      limits: { runs: 1000, tokens: 1_000_000 },
      runs_count: 0,
      tokens_total: 0,
      tokens_by_model: {},
      by_node: [],
      by_flow: [],
      estimated_cost_usd: 0,
      daily: [],
      pct_runs_used: 0,
      pct_tokens_used: 0,
    };
  if (path.startsWith("/billing/flow-deltas")) return [];

  // default: an empty list is the safest generic shape here
  return [];
}

async function installWalkMocks(page: Page): Promise<void> {
  await page.route("**/api/**", (route) => {
    const req = route.request();
    if (req.method() !== "GET") {
      return route.fulfill({ json: { ok: true } });
    }
    return route.fulfill({ json: jsonFor(req.url()) as object });
  });
}

const VIEWS = [
  "Editor",
  "Runs",
  "Activity",
  "Approvals",
  "Trace",
  "Knowledge",
  "Rules",
  "Guide",
  "Team",
  "Channels",
  "Connections",
  "Billing",
];

test("every nav view opens without a crash or console error", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`console.error: ${m.text()}`);
  });

  await seedFakeSession(page);
  await installWalkMocks(page);
  await page.goto("/");

  // wait for the shell (sidebar nav) to be up
  await expect(page.locator(".sidebar")).toBeVisible();

  // the "Admin" nav group is collapsed by default — open it so Team /
  // Channels / Connections / Billing are reachable
  await page.getByRole("button", { name: /Admin/ }).click();

  for (const label of VIEWS) {
    await page.getByRole("button", { name: label, exact: true }).click();
    // the content pane should render *something* (not a bare error / blank)
    const pane = page.locator(".pane, .editor").first();
    await expect(pane, `pane for "${label}"`).toBeVisible();
    await page.waitForTimeout(150);
    expect(errors, `console/page errors after opening "${label}":\n${errors.join("\n")}`).toEqual([]);
  }

  // also the Setup wizard
  await page.getByRole("button", { name: "⚙ Setup" }).click();
  await expect(page.locator(".pane, .editor").first()).toBeVisible();
  expect(errors, `errors after Setup:\n${errors.join("\n")}`).toEqual([]);
});

test("no view clips its content — tall content scrolls inside the fixed frame", async ({ page }) => {
  // Same mocks as the walk, but list endpoints return enough rows to
  // overflow the viewport, so we can tell a real scroll container from a
  // view that just lets .pane's overflow:hidden clip it.
  const many = (n: number) => Array.from({ length: n }, (_, i) => i);
  await seedFakeSession(page);
  await page.route("**/api/**", (route) => {
    const req = route.request();
    if (req.method() !== "GET") return route.fulfill({ json: { ok: true } });
    const path = new URL(req.url()).pathname.replace(/^\/api/, "");
    const base = jsonFor(req.url());
    // fatten the specific lists that feed the clip-prone views
    if (path.startsWith("/audit"))
      return route.fulfill({
        json: many(80).map((i) => ({
          event_id: i, tenant_id: FAKE_TENANT_ID, actor_id: null, actor_email: `u${i}@x.com`,
          action: "flow.publish", target_type: "flow", target_id: `f${i}`,
          summary: `event ${i}`, metadata: {}, created_at: NOW,
        })),
      });
    if (path.startsWith("/review-tasks"))
      return route.fulfill({
        json: many(30).map((i) => ({
          id: `t${i}`, trigger: "sample", case_number: `c${i}`, case_sf_id: null,
          created_at: NOW, status: "open", statement: `flagged statement ${i} `.repeat(6),
          verdict: { salient: [`claim ${i}`] }, contexts: [],
        })),
      });
    if (path.startsWith("/connections") && !path.includes("/actions"))
      return route.fulfill({
        json: many(10).map((i) => ({
          slug: `vendor-${i}`, base_url: `https://api.vendor${i}.com`,
          auth: { type: "bearer" }, has_secret: true,
        })),
      });
    if (path.startsWith("/billing/usage")) {
      const u = base as Record<string, unknown>;
      u.by_flow = many(12).map((i) => ({ flow_id: `f${i}`, name: `Flow ${i}`, runs: i, tokens: i * 100, estimated_cost_usd: i * 0.1 }));
      u.by_node = many(12).map((i) => ({ node: `node_${i}`, tokens: i * 100, estimated_cost_usd: i * 0.1 }));
      u.tokens_by_model = { "gpt-oss-120b": 5000, "gpt-oss-20b": 2500 };
      return route.fulfill({ json: u as object });
    }
    return route.fulfill({ json: base as object });
  });

  await page.goto("/");
  await expect(page.locator(".sidebar")).toBeVisible();
  await page.getByRole("button", { name: /Admin/ }).click();

  for (const label of ["Activity", "Approvals", "Connections", "Billing", "Knowledge", "Runs"]) {
    await page.getByRole("button", { name: label, exact: true }).click();
    await page.waitForTimeout(300);
    const verdict = await page.evaluate(() => {
      const frame = document.querySelector(".pane") as HTMLElement | null;
      if (!frame) return { clipped: true, reason: "no .pane" };
      let contentBottom = 0;
      frame.querySelectorAll<HTMLElement>("*").forEach((e) => {
        contentBottom = Math.max(contentBottom, e.getBoundingClientRect().bottom);
      });
      let scrolls = false;
      frame.querySelectorAll<HTMLElement>("*").forEach((e) => {
        const oy = getComputedStyle(e).overflowY;
        if ((oy === "auto" || oy === "scroll") && e.scrollHeight - e.clientHeight > 8) scrolls = true;
      });
      return { clipped: contentBottom > window.innerHeight + 4 && !scrolls, contentBottom };
    });
    expect(verdict.clipped, `"${label}" clips its overflow (content ${verdict.contentBottom}px, no scroll container)`).toBe(false);
  }
});

test("switching browser tab away and back keeps the current view", async ({ page }) => {
  await seedFakeSession(page);
  await installWalkMocks(page);

  let tenantsCalls = 0;
  await page.route("**/api/tenants", (route) => {
    if (route.request().method() === "GET") tenantsCalls++;
    return route.fulfill({
      json: [{ tenant_id: FAKE_TENANT_ID, role: "owner", name: "Walk Workspace" }],
    });
  });

  await page.goto("/");
  await expect(page.locator(".sidebar")).toBeVisible();

  // navigate away from the default (Editor) view
  await page.getByRole("button", { name: /Admin/ }).click();
  await page.getByRole("button", { name: "Billing", exact: true }).click();
  await expect(page.getByText("free plan")).toBeVisible();
  const callsBefore = tenantsCalls;

  // simulate a Chrome tab switch: hidden -> visible, which makes supabase
  // auth-js re-emit SIGNED_IN via its visibilitychange handler.
  await page.evaluate(async () => {
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await new Promise((r) => setTimeout(r, 50));
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.waitForTimeout(600);

  // still on Billing, and we did NOT refetch the tenant list / unmount the shell
  await expect(page.getByText("free plan")).toBeVisible();
  expect(tenantsCalls, "tenant list should not be refetched on a tab switch").toBe(callsBefore);
});

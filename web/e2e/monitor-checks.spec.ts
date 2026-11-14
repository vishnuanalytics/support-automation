import { test, expect, type Page } from "@playwright/test";
import { installApiMocks, seedFakeSession } from "./mocks";

const queueCase = {
  id: "a", source: "salesforce", case_number: "1001", subject: "Subject 1001", priority: "High",
  owner: "Asha Rao", record_type: "Support", stage: "act", signal: null, latest: null, stale: false,
  overdue_hours: 0, mine: false, links: [], related: [], link_kinds: [], third_parties: [],
  ball: "us", mismatch: false, score: 50, level: "high", next_action: "Reply", missing_link: false,
  overdue: false, stale_hours: 0, duplicate_of: null, ai_state: null, hidden: false, url: null,
};

const checks = [
  {
    id: "c2", check_name: "Publish history", card_id: "1", params: { location_id: 1001, platform: "partner" },
    row_count: 80, summary: "Every publish succeeded.", saved_by: "Claude session",
    created_at: "2026-11-08T18:00:00Z",
    rows: Array.from({ length: 50 }, (_, i) => ({ at: `2026-11-0${(i % 7) + 1}`, status: "success", ref: `r${i}` })),
  },
  {
    id: "c1", check_name: "Order lookup", card_id: "2", params: { order_id: "X1" }, row_count: 0,
    summary: "No order found.", saved_by: "Claude session", created_at: "2026-11-08T17:00:00Z", rows: [],
  },
];

async function mockMonitor(page: Page, withChecks: boolean) {
  await page.route(/\/api\/monitor\/queue/, (route) => route.fulfill({
    json: {
      cases: [queueCase], slack_via_claude: false, slack_read_at: null, has_lead: false, focus_partners: [],
      tiles: {}, trend: [], last_sync: null, running: false, duplicate_groups: {},
      labels: { third_party: "Third party", customer: "Customer" },
    },
  }));
  await page.route(/\/api\/monitor\/alerts/, (route) => route.fulfill({ json: { alerts: [], unread: 0 } }));
  await page.route(/\/api\/monitor\/slack\/status/, (route) => route.fulfill({ json: { connected: false } }));
  await page.route(/\/api\/monitor\/cases\/a(\?.*)?$/, (route) => route.fulfill({
    json: {
      case: { id: "a", case_number: "1001", url: null, children: [], parent: null, brand_stats: null },
      verdict: { level: "high", ball: "us", reasons: [] }, source: "demo", timeline: [], timeline_at: null,
      evidence: [], drafts: [], similar: [], duplicates: [], slack_threads: [],
      slack_team_mention: "", slack_via_claude: false, partner: null, llm_enabled: false,
      labels: { third_party: "Third party", customer: "Customer" },
      brief: { problem: { text: "Problem" }, ball: { text: "Us", mismatch: false }, next_action: "Reply", facts: [] },
      ...(withChecks ? { checks } : {}),
    },
  }));
}

async function openCase(page: Page, withChecks = true) {
  await seedFakeSession(page);
  await installApiMocks(page);
  await mockMonitor(page, withChecks);
  await page.goto("/?view=monitor&tab=cases");
  await page.getByRole("button", { name: "1001" }).click();
}

test("the Checks tab lists saved checks with their rows", async ({ page }) => {
  await openCase(page);
  await page.getByRole("tab", { name: /Checks/ }).click();
  await expect(page.getByRole("heading", { name: "Publish history" })).toBeVisible();
  await expect(page.getByText("Every publish succeeded.")).toBeVisible();
  await expect(page.getByText("location_id 1001 · platform partner", { exact: false })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Order lookup/ })).toContainText("no rows");

  await page.getByText("First 50 of 80 rows").click();
  await expect(page.getByRole("columnheader", { name: "status" })).toBeVisible();
  await expect(page.locator(".monitor-check-rows tbody tr")).toHaveCount(50);
  // Wide rows scroll inside the card, never the page.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});

test("a case without saved checks says how to get one", async ({ page }) => {
  await openCase(page, false);
  await page.getByRole("tab", { name: /Checks/ }).click();
  await expect(page.getByText("No saved checks yet.", { exact: false })).toBeVisible();
});

test("Ask Claude copies a ready prompt for this case", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await openCase(page);
  await page.getByRole("button", { name: "Ask Claude" }).click();
  await expect(page.getByRole("status")).toHaveText('Copied "Ask Claude about this case"');
  const text = await page.evaluate(() => navigator.clipboard.readText());
  expect(text).toContain("case 1001");
  expect(text).toContain("save_check");
});

import { test, expect, type Page } from "@playwright/test";
import { FAKE_TENANT_ID, installApiMocks, seedFakeSession } from "./mocks";

const queueCase = (id: string, number: string) => ({
  id, source: "salesforce", case_number: number, subject: `Subject ${number}`, priority: "High",
  owner: "Asha Rao", record_type: "Support", stage: "act", signal: null, latest: null, stale: false,
  overdue_hours: 0, mine: false, links: [], related: [], link_kinds: [], third_parties: [],
  ball: "us", mismatch: false, score: 50, level: "high", next_action: "Reply", missing_link: false,
  overdue: false, stale_hours: 0, duplicate_of: null, ai_state: null, hidden: false, url: null,
});

async function mockMonitor(page: Page) {
  const reviewed = new Set<string>();
  await page.route(/\/api\/monitor\/queue/, (route) => route.fulfill({
    json: {
      cases: [queueCase("a", "1001"), queueCase("b", "1002")]
        .map((c) => ({ ...c, hidden: reviewed.has(c.id) })),
      slack_via_claude: false, slack_read_at: null, has_lead: false, focus_partners: [], tiles: {},
      trend: [], last_sync: null, running: false, duplicate_groups: {},
      labels: { third_party: "Third party", customer: "Customer" },
    },
  }));
  await page.route(/\/api\/monitor\/alerts/, (route) => route.fulfill({
    json: {
      alerts: [{
        id: 1, case_id: "a", case_number: "1001", subject: "Subject 1001", kind: "new_reply",
        detail: null, created_at: new Date().toISOString(), unread: !reviewed.has("a"),
      }],
      unread: reviewed.has("a") ? 0 : 1,
    },
  }));
  await page.route(/\/api\/monitor\/slack\/status/, (route) => route.fulfill({ json: { connected: false } }));
  await page.route(/\/api\/monitor\/cases\/a\/review/, (route) => {
    reviewed.add("a");
    return route.fulfill({ json: {} });
  });
  await page.route(/\/api\/monitor\/cases\/a(\?.*)?$/, (route) => route.fulfill({
    json: {
      case: { id: "a", case_number: "1001", url: null, children: [], parent: null, brand_stats: null },
      verdict: { level: "high", ball: "us", reasons: [] }, source: "demo", timeline: [], timeline_at: null,
      evidence: [], drafts: [], similar: [], duplicates: [], slack_threads: [],
      slack_team_mention: "", slack_via_claude: false, partner: null, llm_enabled: false,
      labels: { third_party: "Third party", customer: "Customer" },
      brief: { problem: { text: "Problem" }, ball: { text: "Us", mismatch: false }, next_action: "Reply", facts: [] },
    },
  }));
}

const dropDismissFlag = (page: Page) => page.addInitScript((tenantId) => {
  localStorage.removeItem(`onboarding-dismissed:${tenantId}`);
}, FAKE_TENANT_ID);

test("a refresh stays on the page when setup was never dismissed", async ({ page }) => {
  await seedFakeSession(page);
  await installApiMocks(page);
  await mockMonitor(page);
  await dropDismissFlag(page);
  await page.goto("/?view=monitor&tab=queue");
  await expect(page.getByRole("heading", { name: "Case Monitor" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Case Monitor" })).toBeVisible();
  expect(page.url()).toContain("view=monitor");
});

test("a new workspace on the default page lands on setup and records it", async ({ page }) => {
  await seedFakeSession(page);
  await installApiMocks(page);
  await mockMonitor(page);
  await dropDismissFlag(page);
  await page.goto("/");
  await expect(page).toHaveURL(/view=setup/);
  const flag = await page.evaluate((id) => localStorage.getItem(`onboarding-dismissed:${id}`), FAKE_TENANT_ID);
  expect(flag).toBe("1");
});

test("mark reviewed returns to the queue with the case and its alert gone", async ({ page }) => {
  await seedFakeSession(page);
  await installApiMocks(page);
  await mockMonitor(page);
  await page.goto("/?view=monitor&tab=queue");
  await expect(page.getByLabel("More views").locator("option").first()).toHaveText("Older pages (1)");
  await page.getByRole("button", { name: "1001" }).click();
  await page.getByRole("button", { name: "Mark reviewed" }).click();

  await expect(page.getByRole("button", { name: "Refresh now" })).toBeVisible();
  await expect(page.getByRole("button", { name: "1001" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "1002" })).toBeVisible();
  await expect(page.getByText("1 open · 1 reviewed")).toBeVisible();
  await expect(page.getByLabel("More views").locator("option").first()).toHaveText("Older pages");
});

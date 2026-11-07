import { readFileSync } from "node:fs";
import { test, expect, type Page } from "@playwright/test";
import { installApiMocks, seedFakeSession } from "./mocks";

const row = (over: Record<string, unknown>) => ({
  id: "a", case_number: "1001", url: "https://sf/1", subject: "=Menu, stuck", record_type: "DSP",
  owner: "Asha Rao", priority: "High", waiting_on: ["AcmeEats"], party: "third_party", days_silent: 9,
  silent_since: "2026-10-28T00:00:00Z", last_update_at: "2026-10-28T00:00:00Z", last_update_via: "Email",
  last_ack_at: null, our_last_at: "2026-11-05T00:00:00Z", our_follow_ups: 2, checked: ["Email"],
  not_read: [], support_cases: ["2001"], business: "Biz One", biz_id: "111", ...over,
});

async function mockPage(page: Page) {
  await seedFakeSession(page);
  await installApiMocks(page);
  await page.route(/\/api\/monitor\/queue/, (route) => route.fulfill({
    json: {
      cases: [], slack_via_claude: false, slack_read_at: null, has_lead: false, focus_partners: [],
      tiles: {}, trend: [], last_sync: null, running: false, duplicate_groups: {},
      labels: { third_party: "Third party", customer: "Customer" },
    },
  }));
  await page.route(/\/api\/monitor\/alerts/, (route) => route.fulfill({ json: { alerts: [], unread: 0 } }));
  await page.route(/\/api\/monitor\/slack\/status/, (route) => route.fulfill({ json: { connected: false } }));
  await page.route(/\/api\/monitor\/escalations/, (route) => route.fulfill({
    json: {
      days: 5, total: 2,
      summary: [{ name: "AcmeEats", cases: 2, longest_days: 9 }, { name: "Other", cases: 1, longest_days: 6 }],
      cases: [row({}), row({
        id: "b", case_number: "1002", subject: "Second", waiting_on: ["AcmeEats", "Other"], days_silent: 6,
        last_update_at: null, last_update_via: null, not_read: ["Slack"], support_cases: [],
        business: "Biz Two", biz_id: "222",
      })],
    },
  }));
}

test("escalations filter, download as CSV and survive a refresh", async ({ page }) => {
  await mockPage(page);
  await page.goto("/?view=monitor&tab=escalations");
  await expect(page.getByRole("heading", { name: "2 escalations" })).toBeVisible();
  await page.getByLabel("Search business").fill("biz two");
  await expect(page.getByRole("heading", { name: "1 escalation", exact: true })).toBeVisible();
  await page.getByLabel("Search business").fill("222");
  await expect(page.getByRole("heading", { name: "1 escalation", exact: true })).toBeVisible();
  await page.getByLabel("Search business").fill("");
  await expect(page.getByRole("heading", { name: "2 escalations" })).toBeVisible();
  await expect(page.getByText("No update yet")).toBeVisible();
  await expect(page.getByText("Not read: Slack")).toBeVisible();

  await page.getByLabel("Waiting on").selectOption("Other");
  await expect(page.getByRole("heading", { name: "1 escalation", exact: true })).toBeVisible();

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download CSV" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^escalations-other-\d{4}-\d{2}-\d{2}\.csv$/);
  const csv = readFileSync(await download.path(), "utf8").split("\r\n");
  expect(csv).toHaveLength(2);
  expect(csv[0]).toContain("Days without update");
  expect(csv[1]).toContain('1002,Biz Two,222,"AcmeEats, Other",6');

  await page.getByLabel("Waiting on").selectOption("");
  const [all] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download CSV" }).click(),
  ]);
  expect(readFileSync(await all.path(), "utf8")).toContain(`"'=Menu, stuck"`);

  await page.reload();
  await expect(page.getByRole("heading", { name: "2 escalations" })).toBeVisible();
});

import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";
import { installApiMocks, seedFakeSession } from "./mocks";

const issue = (over: Record<string, unknown>) => ({
  squad: "order-squad", issue: "8645", url: "https://github.com/o/r/issues/8645", title: "Vendor sponsorship",
  last_reply_by: "dev-one", days_open: 11, case_id: "a", case_number: "1001", other_cases: ["1002"],
  priority: "Low", sla_due: "2026-10-28T10:05:00Z", sla_status: "Breached by 9d 1h",
  status: "cause_given", tech_status: "Cause given, not closed yet", waiting_on: "third_party",
  reason: "The partner must fix this at source.", over_days: true, ...over,
});

test("engineering issues group by squad, filter, download and survive a refresh", async ({ page }) => {
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
  await page.route(/\/api\/monitor\/tech-pending/, (route) => route.fulfill({
    json: {
      days: 7, total: 3, unread: 0,
      statuses: { no_reply: "No tech reply", cause_given: "Cause given, not closed yet" },
      squads: [
        { squad: "order-squad", issues: [issue({})] },
        { squad: "data-squad", issues: [issue({
          squad: "data-squad", issue: "8690", url: "https://github.com/o/r/issues/8690",
          last_reply_by: null, status: "no_reply", tech_status: "No tech reply", waiting_on: "tech", reason: "",
          other_cases: [],
        }), issue({
          squad: "data-squad", issue: "8738", url: "https://github.com/o/r/issues/8738", days_open: 1, over_days: false,
          last_reply_by: null, status: "no_reply", tech_status: "No tech reply", waiting_on: "tech", reason: "",
          case_id: null, case_number: null, other_cases: [], priority: null, sla_due: null, sla_status: null,
        })] },
      ],
    },
  }));

  await page.goto("/?view=monitor");
  await page.getByRole("button", { name: "Engineering" }).click();
  await expect(page.getByRole("heading", { name: "3 open support issues" })).toBeVisible();
  await expect(page.getByText("No tech reply: 2 · Cause given, not closed yet: 1")).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(2);
  await expect(page.getByRole("cell", { name: "No open case" })).toBeVisible();
  await expect(page.getByText("The partner must fix this at source.")).toBeVisible();
  await expect(page.getByText("Waiting on: Third party")).toBeVisible();

  await page.getByLabel("Age").selectOption("old");
  await expect(page.getByRole("heading", { name: "2 open support issues" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "No open case" })).toHaveCount(0);
  await page.getByLabel("Age").selectOption("");

  await page.getByLabel("Status").selectOption("no_reply");
  await expect(page.getByRole("heading", { name: "2 open support issues" })).toBeVisible();
  await page.getByLabel("Status").selectOption("");

  await page.getByLabel("Squad").selectOption("order-squad");
  await expect(page.getByRole("table")).toHaveCount(1);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download CSV" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^engineering-issues-order-squad-/);
  const lines = readFileSync(await download.path(), "utf8").split("\r\n");
  expect(lines[0]).toContain("#,Squad,Issue #,Title,Last reply by,GH Open since,Case Priority,Support SLA due,Support SLA status");
  expect(lines[0]).toContain("Status,Waiting on,AI note,Case");
  expect(lines[1]).toContain("1,order-squad,8645,Vendor sponsorship,dev-one,11 days,Low,");
  expect(lines[1]).toContain("Cause given, not closed yet");

  await page.reload();
  await expect(page.getByRole("heading", { name: "3 open support issues" })).toBeVisible();
});

import { test, expect, type Page } from "@playwright/test";
import { installApiMocks, seedFakeSession } from "./mocks";

const now = Date.now();
const ago = (days: number) => new Date(now - days * 86400000).toISOString();

const kase = (over: Record<string, unknown>) => ({
  id: "a", stage: "third_party", signal: null, overdue_hours: 0, mine: false, links: [], related: [],
  source: "salesforce", case_number: "00001001", subject: "Menu stuck", priority: "High", owner: "Asha Rao",
  record_type: "DSP", waiting_raw: "Aggregator", sub_status: null, bu: "India", module: "Menu", sub_module: null,
  link_kinds: [], link_from_post: false, partner_channel: null, not_raised: false, waiting_party: "third_party",
  third_parties: ["AcmeEats"], url: "https://sf/a", ball: "third_party", mismatch: false, score: 10, level: "low",
  next_action: "Wait", missing_link: false, overdue: false, stale_hours: null, duplicate_of: null, ai_state: null,
  hidden: false, snooze_until: null, reply_kind: null, customer_reply_kind: null,
  latest: { who: "Us", ours: true, at: ago(5), text: "Following up", url: null, source: "Email", party: "us" },
  ...over,
});

async function mockPage(page: Page) {
  await seedFakeSession(page);
  await installApiMocks(page);
  await page.route(/\/api\/monitor\/queue/, (route) => route.fulfill({
    json: {
      cases: [
        kase({}),
        // The aggregator wrote back after we escalated: flagged so it can be closed.
        kase({ id: "b", case_number: "00001002", subject: "Images missing", record_type: "Support",
               waiting_party: "engineering", stage: "act", reply_kind: "update",
               latest: { who: "AcmeEats KAM", ours: false, at: ago(1), text: "Fixed now", url: null, source: "Email", party: "third_party" } }),
        kase({ id: "c", case_number: "00001003", subject: "Not ours", waiting_party: null, stage: "act" }),
      ],
      slack_via_claude: false, slack_read_at: null, has_lead: false, focus_partners: [], tiles: {}, trend: [],
      last_sync: null, running: false, duplicate_groups: {}, labels: { third_party: "Aggregator", customer: "Merchant" },
    },
  }));
  await page.route(/\/api\/monitor\/alerts/, (route) => route.fulfill({ json: { alerts: [], unread: 0 } }));
  await page.route(/\/api\/monitor\/slack\/status/, (route) => route.fulfill({ json: { connected: false } }));
  await page.route(/\/api\/monitor\/escalation-marks(\?|$)/, (route) => route.fulfill({
    json: { marks: [{ case_id: "b", manual: true, escalated_at: ago(3), escalated_by: "u", note: "KAM by email",
                      responded_at: null, responded_by: null }] },
  }));
}

test("the escalated page lists waiting cases, flags a reply and closes on Team responded", async ({ page }) => {
  await mockPage(page);
  const puts: { url: string; body: unknown }[] = [];
  await page.route(/\/api\/monitor\/escalation-marks\/.+/, (route) => {
    const body = route.request().postDataJSON();
    puts.push({ url: route.request().url(), body });
    const id = decodeURIComponent(route.request().url().split("/escalation-marks/")[1].split("?")[0]);
    route.fulfill({ json: { case_id: id, manual: body.action === "escalate" || id === "b", escalated_at: ago(0), escalated_by: "u",
                            note: body.note ?? null, responded_at: body.action === "responded" ? ago(0) : null, responded_by: "u" } });
  });

  await page.goto("/?view=monitor&tab=escalated");
  await expect(page.getByRole("heading", { name: "Escalated to Aggregator" })).toBeVisible();
  await expect(page.getByRole("radio", { name: "Waiting (2)" })).toBeVisible();
  await expect(page.getByRole("button", { name: "00001003" })).toHaveCount(0);
  // The reply after the escalation sorts first and is flagged.
  const flagged = page.getByRole("row").filter({ hasText: "00001002" });
  await expect(flagged.getByText("Aggregator replied")).toBeVisible();
  await expect(flagged.getByText("KAM by email")).toBeVisible();

  await flagged.getByRole("button", { name: "Team responded" }).click();
  expect(puts[0].body).toEqual({ action: "responded" });
  await expect(page.getByRole("radio", { name: "Waiting (1)" })).toBeVisible();
  await page.getByRole("radio", { name: "Responded (1)" }).click();
  await expect(page.getByRole("button", { name: "00001002" })).toBeVisible();
  await page.getByRole("button", { name: "Reopen" }).click();
  expect(puts[1].body).toEqual({ action: "reopen" });
  await expect(page.getByRole("radio", { name: "Responded (0)" })).toBeVisible();

  await page.getByRole("radio", { name: "Waiting (2)" }).click();
  await page.getByLabel("Case number to add").fill("1003");
  await page.getByRole("button", { name: "Add as escalated" }).click();
  expect(puts[2].body).toEqual({ action: "escalate" });
  await expect(page.getByRole("radio", { name: "Waiting (3)" })).toBeVisible();
});

test("all cases shows where the last reply came from and filters on it", async ({ page }) => {
  await mockPage(page);
  await page.goto("/?view=monitor");
  await page.getByRole("tab", { name: /All/ }).click();
  const strip = page.getByRole("region", { name: "Where the last reply came from" });
  await expect(strip.getByRole("button", { name: /Last reply: Aggregator\s*1/ })).toBeVisible();
  await strip.getByRole("button", { name: /Real update/ }).click();
  await expect(page.getByRole("button", { name: "00001002" })).toBeVisible();
  await expect(page.getByRole("button", { name: "00001001" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Remove Reply tone filter" })).toBeVisible();
});

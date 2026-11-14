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

const quiet = (over: Record<string, unknown>) => ({
  id: "a", case_number: "00001001", url: null, subject: "Menu stuck", record_type: "DSP", owner: "Asha Rao",
  priority: "High", waiting_on: ["AcmeEats"], days_silent: 6, silent_since: ago(6), last_update_at: ago(6),
  last_update_via: "Email", last_ack_at: null, our_last_at: ago(2), our_follow_ups: 1, checked: [], not_read: [],
  support_cases: [], business: "Biz One", biz_id: "111", ...over,
});

async function mockPage(page: Page) {
  await seedFakeSession(page);
  await installApiMocks(page);
  await page.route(/\/api\/monitor\/queue/, (route) => route.fulfill({
    json: {
      cases: [
        kase({}),
        kase({ id: "d", case_number: "00001004", subject: "Price wrong" }),
        // Escalated earlier; the aggregator has since replied.
        kase({ id: "b", case_number: "00001002", subject: "Images missing", stage: "act", reply_kind: "update",
               latest: { who: "AcmeEats KAM", ours: false, at: ago(1), text: "Fixed", url: null, source: "Email", party: "third_party" } }),
        kase({ id: "m", case_number: "00001005", subject: "My own case", stage: "act", mine: true, owner: "Lead",
               customer_reply_kind: "needs_reply",
               latest: { who: "Merchant", ours: false, at: ago(0.2), text: "Any update?", url: null, source: "Email", party: "customer" } }),
        kase({ id: "c", case_number: "00001003", subject: "Agent to close", stage: "close", owner: "Ravi Kumar", waiting_party: "engineering" }),
      ],
      slack_via_claude: false, slack_read_at: null, has_lead: true, focus_partners: [], tiles: {}, trend: [],
      last_sync: null, running: false, duplicate_groups: {}, labels: { third_party: "Aggregator", customer: "Merchant" },
    },
  }));
  await page.route(/\/api\/monitor\/slack\/status/, (route) => route.fulfill({ json: { connected: false } }));
  await page.route(/\/api\/monitor\/escalations/, (route) => route.fulfill({ json: {
    days: 3, total: 2, summary: [], cases: [quiet({}), quiet({ id: "d", case_number: "00001004", subject: "Price wrong", days_silent: 4 })],
  } }));
  await page.route(/\/api\/monitor\/tech-pending/, (route) => route.fulfill({ json: {
    days: 7, total: 2, unread: 0, statuses: {}, squads: [{ squad: "order-squad", issues: [
      { squad: "order-squad", issue: "8686", url: "https://github.com/x/8686", title: "MFR wrong URL", last_reply_by: "us",
        days_open: 20, case_id: "b", case_number: "00001002", other_cases: [], priority: "High", sla_due: null, sla_status: null,
        tech_status: "Cause not confirmed yet", status: "investigating", waiting_on: "tech", reason: "", over_days: true,
        opened_at: ago(20), last_activity_at: ago(4) },
      { squad: "order-squad", issue: "9000", url: "https://github.com/x/9000", title: "Has RCA", last_reply_by: "eng",
        days_open: 9, case_id: null, case_number: null, other_cases: [], priority: null, sla_due: null, sla_status: null,
        tech_status: "Cause given", status: "cause_given", waiting_on: "support", reason: "", over_days: true,
        opened_at: ago(9), last_activity_at: ago(5) },
    ] }],
  } }));
  await page.route(/\/api\/monitor\/escalation-marks(\?|$)/, (route) => route.fulfill({
    json: { marks: [{ case_id: "b", manual: true, escalated_at: ago(3), escalated_by: "u", note: "KAM by email",
                      responded_at: null, responded_by: null }] },
  }));
  await page.route(/\/api\/monitor\/cases\/a(\?.*)?$/, (route) => route.fulfill({ json: {
    partner: { name: "AcmeEats", channel: "email", to_emails: ["help@acme.test"], cc_emails: [], contacts: [],
               portal_url: null, escalation_note: null, notes: null }, drafts: [],
  } }));
}

test("to-do lists what needs the lead and moves a chase to waiting", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await mockPage(page);
  const puts: unknown[] = [];
  await page.route(/\/api\/monitor\/escalation-marks\/.+/, (route) => {
    const body = route.request().postDataJSON();
    puts.push(body);
    const id = decodeURIComponent(route.request().url().split("/escalation-marks/")[1].split("?")[0]);
    route.fulfill({ json: { case_id: id, manual: body.action === "escalate" || id === "b", escalated_at: ago(0),
                            escalated_by: "u", note: null, responded_at: body.action === "responded" ? ago(0) : null, responded_by: "u" } });
  });

  await page.goto("/?view=monitor");
  const mine = page.locator("#todo-mine");
  await expect(mine.getByRole("button", { name: "00001005" })).toBeVisible();
  await expect(mine.getByText("Needs a reply")).toBeVisible();

  // One chase message for both quiet AcmeEats cases.
  const agg = page.locator("#todo-agg");
  await agg.getByRole("button", { name: "Copy chase for all 2" }).click();
  const mail = await page.evaluate(() => navigator.clipboard.readText());
  expect(mail).toContain("Hi AcmeEats team");
  expect(mail).toContain("our ref 00001001");
  expect(mail).toContain("our ref 00001004");
  await expect(agg.getByText("To: help@acme.test")).toBeVisible();

  // Only the issue with no RCA is listed.
  const eng = page.locator("#todo-eng");
  await expect(eng.getByRole("link", { name: "#8686" })).toBeVisible();
  await expect(eng.getByRole("link", { name: "#9000" })).toHaveCount(0);

  // The escalated case that got a reply is flagged.
  const waiting = page.locator("#todo-waiting");
  await expect(waiting.getByText("Aggregator replied")).toBeVisible();
  await expect(waiting.getByText("KAM by email")).toBeVisible();

  // Marking one escalated moves it from chase to waiting.
  await agg.getByRole("listitem").filter({ hasText: "00001004" }).getByRole("button", { name: "Escalated" }).click();
  expect(puts[0]).toEqual({ action: "escalate" });
  await expect(agg.getByRole("button", { name: "00001004" })).toHaveCount(0);
  await expect(waiting.getByRole("button", { name: "00001004" })).toBeVisible();

  await waiting.getByRole("listitem").filter({ hasText: "00001002" }).getByRole("button", { name: "They responded" }).click();
  expect(puts[1]).toEqual({ action: "responded" });
  await expect(waiting.getByRole("button", { name: "00001002" })).toHaveCount(0);

  // Agents: the lead's own case isn't in an agent's list.
  const agents = page.locator("#todo-agents");
  await agents.getByRole("button", { name: /Ravi Kumar/ }).click();
  await expect(agents.getByRole("button", { name: "00001003" })).toBeVisible();
  await agents.getByRole("article").filter({ hasText: "Ravi Kumar" }).getByRole("button", { name: "Copy message to agent" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain("Hi Ravi, please go through these cases today:");
  await expect(agents.getByRole("button", { name: "00001005" })).toHaveCount(0);
});

test("all cases filters by where the last reply came from", async ({ page }) => {
  await mockPage(page);
  await page.goto("/?view=monitor&tab=cases");
  const strip = page.getByRole("region", { name: "Last reply from" });
  await strip.getByRole("button", { name: /Needs a reply/ }).click();
  await expect(page.getByRole("button", { name: "00001005" })).toBeVisible();
  await expect(page.getByRole("button", { name: "00001001" })).toHaveCount(0);
  await expect(page.getByText(/^1$/).first()).toBeVisible();
  await page.getByRole("button", { name: /Clear 2 filters/ }).click();
  await expect(page.getByRole("button", { name: "00001001" })).toBeVisible();
});

import { test, expect, type Page } from "@playwright/test";
import { installApiMocks, seedFakeSession } from "./mocks";

const now = Date.now();
const ago = (hours: number) => new Date(now - hours * 3600000).toISOString();
const msg = (party: string, who: string, hours: number, text: string) =>
  ({ who, ours: party === "us", at: ago(hours), text, url: null, source: party === "engineering" ? "GitHub" : "Email", party });

const kase = (over: Record<string, unknown>) => ({
  id: "a", stage: "act", signal: null, overdue_hours: 0, mine: false, links: [], related: [], source: "salesforce",
  case_number: "00001001", subject: "Menu stuck", priority: "High", owner: "Asha Rao", record_type: "DSP",
  waiting_raw: "Aggregator", sub_status: null, bu: "India", module: "Menu", sub_module: null, link_kinds: [],
  link_from_post: false, partner_channel: null, not_raised: false, waiting_party: "third_party", third_parties: ["AcmeEats"],
  url: null, ball: "us", mismatch: false, score: 10, level: "low", next_action: "Reply", missing_link: false,
  overdue: false, stale_hours: null, duplicate_of: null, ai_state: null, hidden: false, snooze_until: null,
  reply_kind: "update", customer_reply_kind: null, latest: msg("third_party", "AcmeEats KAM", 2, "Fixed on our side"), ...over,
});

async function mockPage(page: Page, views: Record<string, string>, extra: unknown[] = [], issues: unknown[] = []) {
  await seedFakeSession(page);
  await installApiMocks(page);
  await page.route(/\/api\/monitor\/queue/, (route) => route.fulfill({ json: {
    cases: [
      kase({}),
      kase({ id: "e", case_number: "00001002", subject: "MFR not updating", record_type: "Support", waiting_party: "engineering",
             latest: msg("engineering", "abhi-1408", 5, "Root cause found, fix next week") }),
      // Viewed before the latest reply came in, so it's new again.
      kase({ id: "v", case_number: "00001003", subject: "Old viewed", latest: msg("third_party", "Zed", 30, "Second reply") }),
      // Not notifications: an ack, an aggregator reply on a support case, and our own reply.
      kase({ id: "k", case_number: "00001004", signal: { code: "third_party_ack", text: "", source: { label: "" } } }),
      kase({ id: "s", case_number: "00001005", record_type: "Support" }),
      kase({ id: "u", case_number: "00001006", latest: msg("us", "Us", 1, "Chasing") }),
      kase({ id: "seen", case_number: "00001007", subject: "Already seen", stage: "third_party", ball: "third_party",
             latest: msg("third_party", "Zed", 48, "Done") }),
      ...extra,
    ],
    slack_via_claude: false, slack_read_at: null, has_lead: true, focus_partners: [], tiles: {}, trend: [], last_sync: null,
    running: false, duplicate_groups: {}, labels: { third_party: "Aggregator", customer: "Merchant" },
  } }));
  await page.route(/\/api\/monitor\/slack\/status/, (route) => route.fulfill({ json: { connected: false } }));
  await page.route(/\/api\/monitor\/escalations/, (route) => route.fulfill({ json: { days: 3, total: 0, summary: [], cases: [] } }));
  await page.route(/\/api\/monitor\/tech-pending/, (route) => route.fulfill({ json: {
    days: 7, total: issues.length, unread: 0, statuses: {}, squads: [{ squad: "order-squad", issues }] } }));
  await page.route(/\/api\/monitor\/notification-views/, (route) => {
    if (route.request().method() === "POST") return route.fulfill({ json: { viewed_at: new Date().toISOString() } });
    return route.fulfill({ json: { views } });
  });
}

test("new aggregator and engineering replies stay highlighted until marked viewed", async ({ page }) => {
  await mockPage(page, { v: ago(40), seen: ago(1) });
  const posts: unknown[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("notification-views")) posts.push(r.postDataJSON());
  });
  await page.goto("/?view=monitor");
  const tab = page.getByRole("navigation", { name: "Sections" }).getByRole("button", { name: /Notifications/ });
  await expect(tab.getByLabel("3 new")).toBeVisible();
  await tab.click();

  await expect(page.getByRole("heading", { name: "3 new · 3 pending with us" })).toBeVisible();
  const items = page.getByRole("listitem");
  await expect(items).toHaveCount(4);
  for (const n of ["00001004", "00001005", "00001006"]) await expect(page.getByRole("button", { name: n })).toHaveCount(0);
  const seen = items.filter({ hasText: "00001007" });
  await expect(seen).not.toHaveClass(/is-unread/);
  await expect(seen.getByText("Viewed")).toBeVisible();
  await expect(items.filter({ hasText: "00001003" })).toHaveClass(/is-unread/);

  const eng = items.filter({ hasText: "00001002" });
  await expect(eng.getByText("Engineering replied")).toBeVisible();
  await eng.getByRole("button", { name: "Mark viewed" }).click();
  await expect(eng).not.toHaveClass(/is-unread/);
  expect(posts[0]).toEqual({ case_ids: ["e"] });
  await expect(page.getByRole("heading", { name: "2 new · 3 pending with us" })).toBeVisible();

  await page.getByRole("radio", { name: /Aggregator responded/ }).click();
  await page.getByRole("button", { name: "Mark all viewed" }).click();
  expect(posts[1]).toEqual({ case_ids: ["a", "v"] });
  // Viewed clears the highlight, but a reply nobody has acted on stays listed as pending.
  await expect(page.getByRole("heading", { name: "3 pending with us" })).toBeVisible();
  await expect(items.filter({ hasText: "00001001" })).not.toHaveClass(/is-unread/);
  await expect(items.filter({ hasText: "00001001" }).getByText(/^Pending \d+d$/)).toBeVisible();
  await expect(tab.getByLabel(/new/)).toHaveCount(0);
});

const issue = (over: Record<string, unknown>) => ({
  squad: "order-squad", issue: "8742", url: "https://github.com/o/incidents/issues/8742", title: "Discount sent twice",
  last_reply_by: "agent", days_open: 20, case_id: null, case_number: null, other_cases: [], priority: null, sla_due: null,
  sla_status: null, tech_status: "Answered or fix released", status: "answered", waiting_on: "support",
  reason: "Fixed: discount now relays on line items only.", over_days: true, opened_at: ago(480),
  last_activity_at: ago(400), case_ids: [], last_tech_reply_at: ago(420), last_support_reply_at: ago(400),
  reporter: null, biz_id: null, biz_name: null, ...over,
});

test("after time away, an engineering answer nobody passed on is still pending, however old", async ({ page }) => {
  // Engineering answered 17 days ago, an agent said thanks on GitHub, and the case still says waiting on engineering.
  const old = kase({ id: "h", case_number: "00002001", subject: "Discount sent twice", record_type: "Support",
                     stage: "engineering", ball: "engineering", waiting_party: "engineering", owner: "Ravi K",
                     latest: msg("us", "Ravi K", 400, "Thanks, checking with the merchant") });
  await mockPage(page, { h: ago(500) }, [old], [
    issue({ case_ids: ["h"], case_id: "h", case_number: "00002001" }),
    issue({ issue: "8761", url: "https://github.com/o/incidents/issues/8761", title: "Orders not relaying",
            status: "no_reply", tech_status: "No tech reply", waiting_on: "tech", reason: "",
            reporter: "biswadeep.m", biz_id: "97346756", biz_name: "House of Chettinad" }),
  ]);
  await page.goto("/?view=monitor&tab=notifications");
  await page.getByRole("radio", { name: /Pending with us/ }).click();
  const items = page.getByRole("listitem");
  const held = items.filter({ hasText: "00002001" });
  await expect(held).toHaveClass(/is-unread/);
  await expect(held.getByText(/^Pending 17d$/)).toBeVisible();
  await expect(held.getByText(/still says it's waiting on engineering\. Support replied on GitHub/)).toBeVisible();
  await expect(held.getByText("Agent: Ravi K", { exact: false })).toBeVisible();

  await page.getByRole("radio", { name: /No case linked/ }).click();
  const loose = items.filter({ hasText: "Orders not relaying" });
  await expect(loose.getByText(/No open case links this issue\. Raised by biswadeep\.m for House of Chettinad \(97346756\)/)).toBeVisible();
  await expect(loose.getByRole("link", { name: "GitHub #8761" })).toBeVisible();
});

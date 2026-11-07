import { test, expect } from "@playwright/test";
import { installApiMocks, seedFakeSession } from "./mocks";

const card = (over: Record<string, unknown>) => ({
  key: "gh:8709", kind: "chase", title: "#8709: no engineer has replied", why: "Wrong publishing time",
  meta: "menu-squad · Case 1001", chip: "3d 7h", tone: "exception", url: "https://github.com/o/r/issues/8709",
  url_label: "Open issue", case_id: "a", draft: "Hi menu-squad, any update?", draft_label: "Chase message", ...over,
});
const today = {
  cards: [
    card({ key: "dsp:PartnerA", kind: "escalate", title: "Escalate 2 PartnerA cases", chip: "Urgent", url: undefined,
           case_id: undefined, draft: "Hi PartnerA team", draft_label: "Escalation email", to: ["ops@partner.test"] }),
    card({}),
    ...Array.from({ length: 7 }, (_, i) => card({ key: `gh:${i}`, title: `#${i}: looks ready to close`, draft: undefined })),
  ],
  done: 0, clear: 77, open_cases: 96,
  help: { away: { total: 3, answered: 2 }, modules: [["Order Relay", 4]] },
  github: { open: 17, need_you: 8, with_tech: 4 },
  dsp: { total: 35, breached: 30, escalate: 1, on_track: 4 },
  coaching: { days: 30, closed: 166, wrong: 74, share: 45, reasons: [["config-related", 47]],
              agents: [{ agent: "asha", raised: 10, wrong: 4, message: "Hi asha", issues: [] }] },
};
const report = {
  since: "2026-10-30T00:00:00Z",
  sla_start: "2026-11-10T18:30:00Z",
  progress: { since: "2026-11-07", days: 9, measures: [
    { key: "cards", label: "Things on Today, busiest point of the day", start: 19, now: 12, change: "better" },
    { key: "dsp_new_late", label: "New aggregator cases past SLA", start: null, now: null, change: "too_early" },
  ] },
  partners: [{ name: "PartnerA", open: 5, open_late: 4, old_open: 6, old_late: 3, closed: 55, closed_timed: 10, closed_in_time: 8 }],
  rules: [{ name: "Store not opening", escalate_hours: 6, resolve_hours: 24, goal_resolve_hours: 4, closed: 10,
            in_time: 9, share: 90, ready: true }],
  rows: [{ case_id: "a", case_number: "1001", subject: "Store down", owner: "Asha", partner: "PartnerA", priority: "Urgent",
           rule: "Store not opening", raised_at: "2026-11-05T00:00:00Z", deadline: "2026-11-05T04:00:00Z",
           state: "breached", hours_left: -30, last_update_at: null, business: "Biz One", biz_id: "111", new: true },
         { case_id: "b", case_number: "1002", subject: "Menu wrong", owner: "Asha", partner: "PartnerA", priority: "Low",
           rule: "Menu issue", raised_at: "2026-11-05T00:00:00Z", deadline: "2026-11-10T00:00:00Z",
           state: "on_track", hours_left: 50, last_update_at: null, business: "Biz Two", biz_id: "222", new: false }],
};

test("today shows only what needs you, with the next step ready", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await seedFakeSession(page);
  await installApiMocks(page);
  await page.route(/\/api\/monitor\/queue/, (route) => route.fulfill({ json: {
    cases: [], slack_via_claude: false, slack_read_at: null, has_lead: false, focus_partners: [], tiles: {}, trend: [],
    last_sync: null, running: false, duplicate_groups: {}, labels: { third_party: "Third party", customer: "Customer" },
  } }));
  await page.route(/\/api\/monitor\/alerts/, (route) => route.fulfill({ json: { alerts: [], unread: 0 } }));
  await page.route(/\/api\/monitor\/slack\/status/, (route) => route.fulfill({ json: { connected: false } }));
  await page.route(/\/api\/monitor\/today\/done/, (route) => route.fulfill({ status: 204 }));
  await page.route(/\/api\/monitor\/today(\?|$)/, (route) => route.fulfill({ json: today }));
  await page.route(/\/api\/monitor\/sla-report/, (route) => route.fulfill({ json: report }));

  await page.goto("/?view=monitor");
  await expect(page.getByRole("heading", { name: "9 things need you" })).toBeVisible();
  await expect(page.getByText("77 of 96 open cases need nothing from you")).toBeVisible();
  await expect(page.getByText("While you were away, agents raised 3 help requests, and 2 were answered.")).toBeVisible();
  await expect(page.getByText("Order Relay (4)")).toBeVisible();
  await expect(page.getByRole("listitem").filter({ hasText: "looks ready to close" })).toHaveCount(5);
  await page.getByRole("button", { name: "Show 2 more" }).click();
  await expect(page.getByRole("listitem").filter({ hasText: "looks ready to close" })).toHaveCount(7);

  const partner = page.getByRole("listitem").filter({ hasText: "Escalate 2 PartnerA cases" });
  await partner.getByRole("button", { name: "Preview" }).click();
  await expect(partner.getByText("To: ops@partner.test")).toBeVisible();
  await partner.getByRole("button", { name: "Copy escalation email" }).click();
  await expect(partner.getByRole("button", { name: "Copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("Hi PartnerA team");

  const done = page.waitForRequest(/\/api\/monitor\/today\/done/);
  await partner.getByRole("button", { name: "Done" }).click();
  expect((await done).postDataJSON()).toEqual({ key: "dsp:PartnerA" });
  await expect(page.getByRole("heading", { name: "8 things need you" })).toBeVisible();

  await expect(page.getByText("74 of 166 issues closed")).toBeVisible();
  await page.getByRole("button", { name: "Weekly SLA" }).click();
  await expect(page.getByRole("cell", { name: "9 of 10 (90%)" })).toBeVisible();
  await expect(page.getByText("Ready to tighten")).toBeVisible();
  await expect(page.getByRole("cell", { name: "8 of 10" })).toBeVisible();
  await expect(page.getByText("Better")).toBeVisible();
  await expect(page.getByText("Too early")).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Old past SLA" })).toBeVisible();
  await expect(page.getByRole("button", { name: "1002" })).toHaveCount(0);
  await page.getByLabel("Search business").fill("biz two");
  await expect(page.getByRole("button", { name: "1002" })).toBeVisible();
  await expect(page.getByText("On track")).toBeVisible();
  await expect(page.getByRole("button", { name: "1001" })).toHaveCount(0);
  await page.getByLabel("Search business").fill("111");
  await expect(page.getByRole("button", { name: "1001" })).toBeVisible();
  await page.getByLabel("Search business").fill("no such business");
  await expect(page.getByText("Nothing matches")).toBeVisible();
  await page.getByLabel("Search business").fill("");

  await page.getByLabel("More views").selectOption("engineering");
  await expect(page).toHaveURL(/tab=engineering/);
});

import { test, expect } from "@playwright/test";
import { installApiMocks, seedFakeSession } from "./mocks";

const card = (over: Record<string, unknown>) => ({
  key: "gh:8709", kind: "chase", title: "#8709: no engineer has replied", why: "Wrong publishing time",
  meta: "menu-squad · Case 1001", chip: "3d 7h", tone: "exception", url: "https://github.com/o/r/issues/8709",
  url_label: "Open issue", case_id: "a", draft: "Hi menu-squad, any update?", draft_label: "Chase message", group: "sla",
  cases: [{ case_id: "a", case_number: "1001", subject: "Store down", owner: "Asha",
            latest: { who: "Ravi", ours: false, at: "2026-11-05T00:00:00Z", text: "We are checking", url: null,
                      source: "owner/repo#8709", party: "engineering" } }],
  ...over,
});
const today = {
  cards: [
    card({ key: "reply:Asha", kind: "reply", group: "replies", title: "Asha: 2 replies not acted on", chip: "3d 2h ago", url: undefined,
           case_id: undefined, draft: "Hi Asha", draft_label: "Message for the agent",
           cases: [{ case_id: "a", case_number: "1001", subject: "Store down", owner: "Asha", step: "Push PartnerA for a real answer", latest: { who: "PartnerA", ours: false, at: "2026-11-05T00:00:00Z", text: "Item was in stock", url: null, source: "DSP email", party: "third_party" } },
                   { case_id: "b", case_number: "1002", subject: "Menu wrong", owner: "Asha", latest: null }] }),
    card({ key: "dsp:PartnerA", kind: "escalate", title: "Escalate 2 PartnerA cases", chip: "Urgent", url: undefined,
           case_id: undefined, draft: "Hi PartnerA team", draft_label: "Escalation email", to: ["ops@partner.test"] }),
    card({}),
    card({ key: "logs:PartnerA", kind: "logs", group: "agents", title: "Save logs for 1 PartnerA case", chip: "0.5d left", url: undefined,
           case_id: undefined, draft: "Please save the logs", draft_label: "Message for the agents" }),
  ],
  done: 0, clear: 77, open_cases: 96,
  help: { away: { total: 3, answered: 2 }, modules: [["Order Relay", 4]] },
  github: { open: 17, need_you: 8, with_tech: 4 },
  dsp: { total: 35, breached: 30, escalate: 1, on_track: 4 },
  coaching: { days: 30, closed: 166, wrong: 74, share: 45, reasons: [["config-related", 47]],
              agents: [{ agent: "asha", raised: 10, wrong: 4, message: "Hi asha", issues: [] }] },
};
const weekly = {
  week_start: "2026-11-11T00:00:00+05:30", week_label: "Wed 11 Nov", as_of: "2026-11-11T04:00:00Z",
  engineering: {
    total: 1, message: "Hi team, one issue", left_out: { "No open case": 3, "Waiting on support": 1 },
    squads: [{ squad: "menu-squad", message: "Hi team, menu", issues: [{
      issue: "8709", title: "Wrong time", url: "https://github.com/o/r/issues/8709", squad: "menu-squad", last_reply_by: "ravi",
      last_activity_at: "2026-11-05T00:00:00Z", days_open: 6, case_id: "a", case_number: "1001", tech_status: "No tech reply",
      businesses: [{ business: "Biz One", biz_id: "111" }] }] }],
  },
  dsp: { total: 1, partners: [{ name: "PartnerA", message: "Hi PartnerA team, list", rows: [{
    case_id: "b", case_number: "1002", subject: "Menu wrong", business: "Biz Two", biz_id: "222", quiet_days: 9, new: true,
    latest: { who: "asha", ours: true, at: "2026-11-02T00:00:00Z", text: "Any update?", url: null, source: "DSP email", party: "us" } }] }] },
};
const agents = {
  days: 30, raised: 8, wrong: 2, share: 25,
  agents: [{ agent: "Asha", raised: 5, wrong: 2, main_mistake: "Wrong store's payload", message: "Hi Asha, please look",
             cases: [{ case_id: "a", case_number: "1001", subject: "Store down", raised_at: "2026-11-05T00:00:00Z", check: [],
                       problems: [{ kind: "Wrong store's payload", text: "Payload is for location 777, but the case is 100" }] }],
             check: [{ case_id: "b", case_number: "1002", subject: "Menu wrong", raised_at: "2026-11-05T00:00:00Z", problems: [],
                       check: ["Our records have no PartnerA ID for location 300, so A9 can't be confirmed"] }],
             github: [{ title: "Config issue", url: "https://github.com/o/r/issues/1", reasons: ["config-related"] }] },
           { agent: "Ravi", raised: 3, wrong: 0, main_mistake: null, message: null, cases: [], check: [], github: [] }],
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
  await page.route(/\/api\/monitor\/weekly/, (route) => route.fulfill({ json: weekly }));
  await page.route(/\/api\/monitor\/agents/, (route) => route.fulfill({ json: agents }));

  await page.goto("/?view=monitor");
  await expect(page.getByRole("heading", { name: "4 things need you" })).toBeVisible();
  await expect(page.getByText("77 of 96 open cases need nothing from you")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Replies waiting (1)" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "No response past SLA (2)" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Agents (1)" })).toBeVisible();

  const replies = page.getByRole("listitem").filter({ hasText: "Asha: 2 replies not acted on" }).first();
  await replies.getByRole("button", { name: "Show 2 cases" }).click();
  await expect(replies.getByText("Item was in stock")).toBeVisible();
  await expect(replies.getByText("Push PartnerA for a real answer")).toBeVisible();
  await expect(replies.getByText("PartnerA · DSP email")).toBeVisible();
  await expect(replies.getByText("No response found")).toBeVisible();

  const partner = page.getByRole("listitem").filter({ hasText: "Escalate 2 PartnerA cases" });
  await partner.getByRole("button", { name: "Preview" }).click();
  await expect(partner.getByText("To: ops@partner.test")).toBeVisible();
  await partner.getByRole("button", { name: "Copy escalation email" }).click();
  await expect(partner.getByRole("button", { name: "Copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("Hi PartnerA team");

  const done = page.waitForRequest(/\/api\/monitor\/today\/done/);
  await partner.getByRole("button", { name: "Done" }).click();
  expect((await done).postDataJSON()).toEqual({ key: "dsp:PartnerA" });
  await expect(page.getByRole("heading", { name: "3 things need you" })).toBeVisible();

  await page.getByRole("button", { name: "Wednesday Report" }).click();
  await expect(page.getByRole("heading", { name: "Pending from engineering (1)" })).toBeVisible();
  await expect(page.getByText("Week from Wed 11 Nov.")).toBeVisible();
  await expect(page.getByRole("cell", { name: /ravi · GitHub/ })).toBeInViewport();
  await expect(page.getByText("Left out: No open case (3), Waiting on support (1).")).toBeVisible();
  await page.getByRole("button", { name: "Copy full message" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("Hi team, one issue");
  await expect(page.getByRole("heading", { name: "Aggregators past SLA, no response (1)" })).toBeVisible();
  await page.getByRole("button", { name: "Copy PartnerA message" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("Hi PartnerA team, list");

  await page.getByRole("button", { name: "Agents" }).click();
  await expect(page.getByRole("heading", { name: "2 of 8 raises had a problem" })).toBeVisible();
  await page.getByRole("button", { name: "Asha" }).click();
  await expect(page.getByText("Payload is for location 777, but the case is 100")).toBeVisible();
  await expect(page.getByText("can't be confirmed")).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy message" })).toBeInViewport();
  await page.getByRole("button", { name: "Copy message" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("Hi Asha, please look");

  await page.getByLabel("More views").selectOption("sla");
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

import { test, expect } from "@playwright/test";
import { installApiMocks, seedFakeSession } from "./mocks";

const report = {
  days: 30, raised: 3, wrong: 1, share: 33,
  agents: [{
    agent: "Asha Rao", raised: 3, wrong: 1, main_mistake: "Missing details", message: "Hi Asha Rao, …",
    github: [],
    cases: [{ case_id: "e1", case_number: "2001", subject: "Orders not reaching POS", raised_at: "2026-11-12T05:00:00Z",
              source: "engineering", problems: [{ kind: "Missing details", text: "Missing order ID" }], check: [] }],
    check: [{ case_id: "d1", case_number: "2002", subject: "Store won't open", raised_at: "2026-11-12T04:00:00Z",
              source: "aggregator", problems: [],
              check: ["STR-01 Store action history found nothing: No store on/off action in the window (location_id 1)"] }],
  }],
};

test("the Agents page tags engineering raises and lists empty checks as please check", async ({ page }) => {
  await seedFakeSession(page);
  await installApiMocks(page);
  await page.route(/\/api\/monitor\/queue/, (r) => r.fulfill({ json: { cases: [], tiles: {}, trend: [], focus_partners: [],
    duplicate_groups: {}, labels: { third_party: "Third party", customer: "Customer" } } }));
  await page.route(/\/api\/monitor\/alerts/, (r) => r.fulfill({ json: { alerts: [], unread: 0 } }));
  await page.route(/\/api\/monitor\/slack\/status/, (r) => r.fulfill({ json: { connected: false } }));
  await page.route(/\/api\/monitor\/agents/, (r) => r.fulfill({ json: report }));
  await page.goto("/?view=monitor&tab=agents");

  await expect(page.getByRole("heading", { name: "1 of 3 raises had a problem" })).toBeVisible();
  await page.getByRole("button", { name: "Asha Rao" }).click();
  const eng = page.getByRole("listitem").filter({ hasText: "2001" });
  await expect(eng.getByText("Engineering", { exact: true })).toBeVisible();
  await expect(eng.getByText("Missing order ID")).toBeVisible();
  const dsp = page.getByRole("listitem").filter({ hasText: "2002" });
  await expect(dsp.getByText("Engineering", { exact: true })).toHaveCount(0);
  await expect(dsp.getByText("Please check")).toBeVisible();
  await expect(dsp.getByText(/STR-01 Store action history found nothing/)).toBeVisible();
});

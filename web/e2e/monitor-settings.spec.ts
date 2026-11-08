import { test, expect, type Page } from "@playwright/test";
import { installApiMocks, seedFakeSession } from "./mocks";

async function mockSettings(page: Page, config: Record<string, unknown>) {
  const puts: Record<string, unknown>[] = [];
  await page.route(/\/api\/monitor\/queue/, (route) => route.fulfill({
    json: {
      cases: [], slack_via_claude: false, slack_read_at: null, has_lead: false, focus_partners: [],
      tiles: {}, trend: [], last_sync: null, running: false, duplicate_groups: {},
      labels: { third_party: "Third party", customer: "Customer" },
    },
  }));
  await page.route(/\/api\/monitor\/alerts/, (route) => route.fulfill({ json: { alerts: [], unread: 0 } }));
  await page.route(/\/api\/monitor\/slack\/status/, (route) => route.fulfill({ json: { connected: false } }));
  await page.route(/\/api\/monitor\/config/, async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON().config;
      puts.push(body);
      config = body;
    }
    await route.fulfill({ json: { config } });
  });
  return puts;
}

const BASE = { enabled: true, auto_checks: [{ modules: ["Orders"], checks: ["order"] }],
               auto_checks_dsp: true, auto_checks_engineering: false };

test("the DSP and engineering scans switch on and off from Settings", async ({ page }) => {
  await seedFakeSession(page);
  await installApiMocks(page);
  const puts = await mockSettings(page, { ...BASE });
  await page.goto("/?view=monitor&tab=settings");

  const dsp = page.getByRole("switch", { name: "New aggregator (DSP) cases" });
  const eng = page.getByRole("switch", { name: "New engineering cases" });
  await expect(page.getByText("1 rule in auto_checks")).toBeVisible();
  await expect(dsp).toBeChecked();
  await expect(eng).not.toBeChecked();

  await page.getByText("New engineering cases", { exact: true }).click();
  await expect(page.getByText("New engineering cases: checks on")).toBeVisible();
  await expect(eng).toBeChecked();
  await page.getByText("New aggregator (DSP) cases", { exact: true }).click();
  await expect(dsp).not.toBeChecked();
  expect(puts.at(-1)).toMatchObject({ enabled: true, auto_checks_dsp: false, auto_checks_engineering: true });
  await expect(page.getByLabel("Monitor settings JSON")).toHaveValue(/"auto_checks_dsp": false/);
});

test("the switches wait for valid JSON", async ({ page }) => {
  await seedFakeSession(page);
  await installApiMocks(page);
  await mockSettings(page, { ...BASE });
  await page.goto("/?view=monitor&tab=settings");
  await page.getByLabel("Monitor settings JSON").fill("{ broken");
  await expect(page.getByRole("switch", { name: "New engineering cases" })).toBeDisabled();
  await expect(page.getByText("Fix the JSON below to change these.")).toBeVisible();
});

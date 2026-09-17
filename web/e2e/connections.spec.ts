import { test, expect } from "@playwright/test";
import { installApiMocks, seedFakeSession, FAKE_TENANT_ID } from "./mocks";

/**
 * Multi-provider connectors steps 1 + 2 — the "Case system" picker and the
 * Zendesk connection panel on the Connections tab. Mocks every /api/** call;
 * no real Salesforce org, Zendesk account, or backend.
 */

test.beforeEach(async ({ page }) => {
  await seedFakeSession(page);
});

test("pick Zendesk as the case system and connect an account", async ({ page }) => {
  await installApiMocks(page);

  let caseConnector = "salesforce";
  let zendeskConfigured = false;
  let savedZendesk: Record<string, unknown> | null = null;

  // panels this view also renders, that this test doesn't otherwise care about
  await page.route("**/api/connections*", (route) =>
    route.fulfill({ json: route.request().method() === "GET" ? [] : {} }));
  await page.route("**/api/integrations/salesforce*", (route) =>
    route.fulfill({ json: route.request().method() === "GET" ? [] : {} }));
  await page.route("**/api/integrations/llm*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, tenant: { groq: false, anthropic: false, openrouter: false }, platform: { groq: true, anthropic: false, openrouter: false } } }));
  await page.route("**/api/tenants/case-taxonomy*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, config: {}, updated_at: null, defaults: {} } }));
  await page.route("**/api/tenants/channel-connector-map*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, channel_connector_map: {} } }));

  await page.route("**/api/tenants/case-connector*", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, case_connector: caseConnector } });
    }
    const body = route.request().postDataJSON() as Record<string, unknown>;
    caseConnector = body.case_connector as string;
    return route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, case_connector: caseConnector } });
  });

  await page.route("**/api/integrations/zendesk/test", (route) =>
    route.fulfill({ json: { ok: true, error: null } }));

  await page.route("**/api/integrations/zendesk*", (route) => {
    const method = route.request().method();
    if (method === "GET") {
      return route.fulfill({
        json: zendeskConfigured
          ? { tenant_id: FAKE_TENANT_ID, configured: true, status: "active",
             subdomain: "acme", email: "bot@acme.com" }
          : { tenant_id: FAKE_TENANT_ID, configured: false, status: "none" },
      });
    }
    if (method === "PUT") {
      savedZendesk = route.request().postDataJSON() as Record<string, unknown>;
      zendeskConfigured = true;
      return route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, configured: true, status: "active",
                                     subdomain: savedZendesk.subdomain, email: savedZendesk.email } });
    }
    return route.fallback();
  });

  await page.goto("/");
  await page.getByRole("button", { name: /Admin/ }).click();
  await page.getByRole("button", { name: "Connections" }).click();

  await expect(page.getByRole("heading", { name: "Case system" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Zendesk" })).toBeVisible();

  // connect Zendesk first
  const zdPanel = page.locator(".int-card").filter({ hasText: "Zendesk admin console" }).last();
  await zdPanel.getByPlaceholder("subdomain (yourcompany)").fill("acme");
  await zdPanel.getByPlaceholder("agent email (bot@yourcompany.com)").fill("bot@acme.com");
  await zdPanel.getByPlaceholder("API token").fill("test-token");
  await zdPanel.getByRole("button", { name: "Save" }).click();
  await expect(zdPanel.getByText("saved", { exact: true })).toBeVisible();
  expect(savedZendesk).toBeTruthy();
  expect((savedZendesk as Record<string, unknown>).subdomain).toBe("acme");

  // now switch the case system to it
  await page.getByTestId("case-connector-select").selectOption("zendesk");
  await expect(page.getByText("saved", { exact: true }).first()).toBeVisible();
  expect(caseConnector).toBe("zendesk");
});

test("pick HubSpot as the case system and connect an account", async ({ page }) => {
  await installApiMocks(page);

  let caseConnector = "salesforce";
  let hubspotConfigured = false;
  let savedHubSpot: Record<string, unknown> | null = null;

  await page.route("**/api/connections*", (route) =>
    route.fulfill({ json: route.request().method() === "GET" ? [] : {} }));
  await page.route("**/api/integrations/salesforce*", (route) =>
    route.fulfill({ json: route.request().method() === "GET" ? [] : {} }));
  await page.route("**/api/integrations/zendesk*", (route) =>
    route.fulfill({ json: route.request().method() === "GET"
      ? { tenant_id: FAKE_TENANT_ID, configured: false, status: "none" } : {} }));
  await page.route("**/api/integrations/llm*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, tenant: { groq: false, anthropic: false, openrouter: false }, platform: { groq: true, anthropic: false, openrouter: false } } }));
  await page.route("**/api/tenants/case-taxonomy*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, config: {}, updated_at: null, defaults: {} } }));
  await page.route("**/api/tenants/channel-connector-map*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, channel_connector_map: {} } }));

  await page.route("**/api/tenants/case-connector*", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, case_connector: caseConnector } });
    }
    const body = route.request().postDataJSON() as Record<string, unknown>;
    caseConnector = body.case_connector as string;
    return route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, case_connector: caseConnector } });
  });

  await page.route("**/api/integrations/hubspot/test", (route) =>
    route.fulfill({ json: { ok: true, error: null, portal_id: "12345" } }));

  await page.route("**/api/integrations/hubspot*", (route) => {
    const method = route.request().method();
    if (method === "GET") {
      return route.fulfill({
        json: hubspotConfigured
          ? { tenant_id: FAKE_TENANT_ID, configured: true, status: "active", portal_id: "12345" }
          : { tenant_id: FAKE_TENANT_ID, configured: false, status: "none" },
      });
    }
    if (method === "PUT") {
      savedHubSpot = route.request().postDataJSON() as Record<string, unknown>;
      hubspotConfigured = true;
      return route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, configured: true, status: "active",
                                     portal_id: "12345" } });
    }
    return route.fallback();
  });

  await page.goto("/");
  await page.getByRole("button", { name: /Admin/ }).click();
  await page.getByRole("button", { name: "Connections" }).click();

  await expect(page.getByRole("heading", { name: "Case system" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "HubSpot" })).toBeVisible();

  // connect HubSpot first
  const hsPanel = page.locator(".int-card").filter({ hasText: "Private Apps" }).last();
  await hsPanel.getByPlaceholder("Private App access token").fill("test-token");
  await hsPanel.getByRole("button", { name: "Save" }).click();
  await expect(hsPanel.getByText("saved", { exact: true })).toBeVisible();
  expect(savedHubSpot).toBeTruthy();
  expect((savedHubSpot as Record<string, unknown>).access_token).toBe("test-token");

  // now switch the case system to it
  await page.getByTestId("case-connector-select").selectOption("hubspot");
  await expect(page.getByText("saved", { exact: true }).first()).toBeVisible();
  expect(caseConnector).toBe("hubspot");
});

test("route a channel to a different connector than the case system default", async ({ page }) => {
  await installApiMocks(page);

  let map: Record<string, string> = {};

  await page.route("**/api/connections*", (route) =>
    route.fulfill({ json: route.request().method() === "GET" ? [] : {} }));
  await page.route("**/api/integrations/salesforce*", (route) =>
    route.fulfill({ json: route.request().method() === "GET" ? [] : {} }));
  await page.route("**/api/integrations/zendesk*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, configured: false, status: "none" } }));
  await page.route("**/api/integrations/hubspot*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, configured: false, status: "none" } }));
  await page.route("**/api/integrations/llm*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, tenant: { groq: false, anthropic: false, openrouter: false }, platform: { groq: true, anthropic: false, openrouter: false } } }));
  await page.route("**/api/tenants/case-taxonomy*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, config: {}, updated_at: null, defaults: {} } }));
  await page.route("**/api/tenants/case-connector*", (route) =>
    route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, case_connector: "salesforce" } }));

  await page.route("**/api/tenants/channel-connector-map*", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, channel_connector_map: map } });
    }
    const body = route.request().postDataJSON() as Record<string, unknown>;
    map = body.channel_connector_map as Record<string, string>;
    return route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, channel_connector_map: map } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: /Admin/ }).click();
  await page.getByRole("button", { name: "Connections" }).click();

  await expect(page.getByRole("heading", { name: "Route by channel" })).toBeVisible();

  const panel = page.locator(".int-card").filter({ hasText: "Route by channel" });
  await page.getByTestId("channel-connector-hubspot").selectOption("hubspot");
  await panel.getByRole("button", { name: "Save" }).click();
  await expect(panel.getByText("saved", { exact: true })).toBeVisible();
  expect(map).toEqual({ hubspot: "hubspot" });
});

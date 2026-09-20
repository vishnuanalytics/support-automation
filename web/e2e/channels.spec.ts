import { test, expect } from "@playwright/test";
import { installApiMocks, seedFakeSession, FAKE_TENANT_ID } from "./mocks";

/**
 * Multi-provider connectors step 3 — the Freshchat channel-connection UI
 * (ChannelsView). Mocks every /api/** call; no real Freshchat account, no
 * real backend.
 */

test.beforeEach(async ({ page }) => {
  await seedFakeSession(page);
});

test("connect a Freshchat channel from the Connections tab", async ({ page }) => {
  await installApiMocks(page);

  let saved: Record<string, unknown> | null = null;
  let freshchatConfigured = false;

  await page.route("**/api/integrations/email*", (route) => {
    if (route.request().method() !== "GET") return route.fulfill({ json: { configured: false } });
    return route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, configured: false, status: "none",
                                   gmail_available: false } });
  });

  await page.route("**/api/integrations/freshchat/webhook-url*", (route) =>
    route.fulfill({ json: { url: `https://api.example.test/webhooks/freshchat/${FAKE_TENANT_ID}` } }));

  await page.route("**/api/integrations/freshchat/test", (route) =>
    route.fulfill({ json: { ok: true, error: null } }));

  await page.route("**/api/integrations/freshchat*", (route) => {
    const method = route.request().method();
    if (method === "GET") {
      return route.fulfill({
        json: freshchatConfigured
          ? { tenant_id: FAKE_TENANT_ID, configured: true, status: "active",
             domain: "acme.freshchat.com", team: "support", auto_send_enabled: false,
             signature_verification: true }
          : { tenant_id: FAKE_TENANT_ID, configured: false, status: "none" },
      });
    }
    if (method === "PUT") {
      saved = route.request().postDataJSON() as Record<string, unknown>;
      freshchatConfigured = true;
      return route.fulfill({ json: { tenant_id: FAKE_TENANT_ID, configured: true, status: "active",
                                     domain: saved.domain, team: saved.team,
                                     auto_send_enabled: saved.auto_send_enabled,
                                     signature_verification: true } });
    }
    return route.fallback();
  });

  await page.goto("/");

  // expand the collapsed "Admin" nav group, then open Connections (the nav
  // label was renamed from "Channels" at some point -- the underlying
  // component/view slug/directory are still channels/ChannelsView.tsx and
  // "connections" respectively, just the visible label changed)
  await page.getByRole("button", { name: /Admin/ }).click();
  await page.getByRole("button", { name: "Connections", exact: true }).click();

  // the Connections page now splits into "Integrations" / "Channels"
  // sub-sections (a radiogroup, added after this test was first written) --
  // the Freshchat panel lives under "Channels", not the default
  // "Integrations" one.
  await page.getByRole("radio", { name: "Channels" }).click();

  // the email panel shares field placeholders/button text with the
  // Freshchat one (both render on this same "Channels" sub-section) --
  // scope every interaction to the Freshchat panel specifically via its
  // own `.int-card` wrapper (ChannelsView.tsx), not the outer `.pane` the
  // whole sub-section shares (which matched both panels' "Save" buttons).
  const panel = page.locator(".int-card").filter({ hasText: "Freshchat channel" });
  await expect(panel).toBeVisible();

  await panel.getByPlaceholder("yourcompany.freshchat.com").fill("acme.freshchat.com");
  await panel.getByPlaceholder("API token", { exact: true }).fill("test-api-token");
  await panel.getByRole("button", { name: "Save" }).click();

  await expect(panel.getByText("saved", { exact: true })).toBeVisible();
  expect(saved).toBeTruthy();
  expect((saved as Record<string, unknown>).domain).toBe("acme.freshchat.com");
  expect((saved as Record<string, unknown>).api_token).toBe("test-api-token");

  // the webhook URL for this tenant is shown for pasting into Freshchat
  await expect(panel.locator(`input[value*="/webhooks/freshchat/${FAKE_TENANT_ID}"]`)).toBeVisible();

  // test connection round-trips through the (mocked) endpoint
  await panel.getByRole("button", { name: "Test connection" }).click();
  await expect(panel.getByText("connection ok")).toBeVisible();
});

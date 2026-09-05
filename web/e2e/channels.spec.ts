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

test("connect a Freshchat channel from the Channels tab", async ({ page }) => {
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

  // expand the collapsed "Admin" nav group, then open Channels
  await page.getByRole("button", { name: /Admin/ }).click();
  await page.getByRole("button", { name: "Channels" }).click();

  // the email panel shares field placeholders/button text with the
  // Freshchat one (both render on this same tab) -- scope every
  // interaction to the Freshchat panel specifically. `.last()`: App.tsx's
  // own outer content wrapper is ALSO `.pane` and (containing both panels)
  // also matches `hasText` — the innermost/last match in document order is
  // the actual Freshchat panel div.
  const panel = page.locator(".pane").filter({ hasText: "Freshchat channel" }).last();
  await expect(panel).toBeVisible();

  await panel.getByPlaceholder("yourcompany.freshchat.com").fill("acme.freshchat.com");
  await panel.getByPlaceholder("••••••••••••").fill("test-api-token");
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

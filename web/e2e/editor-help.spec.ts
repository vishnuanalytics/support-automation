import { test, expect } from "@playwright/test";
import { installApiMocks, seedFakeSession, FAKE_FLOW_ID, FAKE_TENANT_ID } from "./mocks";

/**
 * User: "we need to give good guidance and knowledge on the support
 * automation dashboard. How to use this product very good" — clarified via
 * AskUserQuestion into both in-app contextual help and a standalone guide,
 * scoped first to the flow editor (where this session's simplification work
 * has focused, and where users report the most confusion). This covers the
 * in-app half: a "❓ Help" button in the editor toolbar (EditorHelp.tsx)
 * opening a quick-reference SlideOver. The standalone half is
 * docs/DASHBOARD_GUIDE.md, which isn't browser-testable.
 */

test.beforeEach(async ({ page }) => {
  await seedFakeSession(page);
});

test("Help button opens a quick-reference panel covering the editor's own controls", async ({ page }) => {
  await installApiMocks(page);

  const flow = {
    flow_id: FAKE_FLOW_ID, tenant_id: FAKE_TENANT_ID, team: "support",
    name: "E2E Test Flow", status: "draft", version: 1, published_version: null,
    sf_entry: false,
    nodes: [
      { node_id: "n1", type: "confidence_gate", label: "Gate", position_x: 100, position_y: 100, config: {} },
      { node_id: "n2", type: "auto_reply", label: "Auto reply", position_x: 400, position_y: 100, config: {} },
    ],
    edges: [{ edge_id: "e1", source_node_id: "n1", target_node_id: "n2", condition: {} }],
  };
  await page.route(`**/api/flows/${FAKE_FLOW_ID}`, (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: flow });
    return route.fulfill({ json: flow });
  });
  await page.route("**/api/case-connector/meta*", (route) =>
    route.fulfill({ json: { available: false, queues: [], case_types: [], modules: [], case_fields: [] } }));

  await page.goto("/");
  await page.locator(".flow-item", { hasText: "E2E Test Flow" }).click();
  await page.getByRole("button", { name: "🗺️ Graph" }).click();
  await page.getByText("Gate", { exact: true }).waitFor();

  // closed by default -- doesn't get in the way of someone who already
  // knows the editor
  await expect(page.locator(".ui-slideover__title", { hasText: "How to use this editor" })).not.toBeVisible();

  await page.getByRole("button", { name: "❓ Help" }).click();
  await expect(page.locator(".ui-slideover__title", { hasText: "How to use this editor" })).toBeVisible();

  // covers the screen's actual controls, not generic filler -- each of
  // these section headers names something a user can click right here
  for (const heading of [
    "Three ways to build a flow",
    "Reading the canvas",
    "Conditions, without writing code",
    "Test before you publish",
    "Saving, publishing, undoing",
    "After you publish, check",
  ]) {
    await expect(page.getByText(heading)).toBeVisible();
  }

  // points to the full written guide rather than trying to be the whole manual
  await expect(page.getByText(/DASHBOARD_GUIDE\.md/)).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(page.locator(".ui-slideover__title", { hasText: "How to use this editor" })).not.toBeVisible();
});

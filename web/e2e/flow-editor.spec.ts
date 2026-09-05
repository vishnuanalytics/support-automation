import { test, expect } from "@playwright/test";
import { installApiMocks, seedFakeSession } from "./mocks";

/**
 * Phase 9's original plan for this suite ("stubbed auth -> load flow -> add
 * node -> Save") was never actually built (see PROJECT_SCOPE.md's Phase 9
 * residual notes) — every real bug in the picker/editor work since then was
 * only ever found by a human clicking through manually. This is the first
 * real automated pass.
 */

test.beforeEach(async ({ page }) => {
  await seedFakeSession(page);
});

test("stubbed auth -> load a flow -> add a node -> save the draft", async ({ page }) => {
  let savedBody: { nodes?: unknown[] } | null = null;
  await installApiMocks(page, { onSave: (b) => (savedBody = b) });

  await page.goto("/");

  // signed in (fake session), one workspace (auto-selected), lands on the
  // Editor nav with the seeded flow in the sidebar list
  const flowItem = page.locator(".flow-item", { hasText: "E2E Test Flow" });
  await expect(flowItem).toBeVisible();
  await flowItem.click();

  // the canvas rendered the flow's one seeded `retrieve` node
  await expect(page.getByText("retrieve", { exact: true }).first()).toBeVisible();

  // Save draft starts disabled — nothing to save yet
  const saveBtn = page.getByRole("button", { name: "Save draft" });
  await expect(saveBtn).toBeDisabled();

  // add a node via the palette
  await page.getByRole("button", { name: "＋ add node" }).click();
  await page.getByTitle("add a classify node").click();
  await expect(page.getByText("classify", { exact: true }).first()).toBeVisible();

  // adding a node marks the draft dirty
  await expect(saveBtn).toBeEnabled();
  await saveBtn.click();

  await expect(page.getByText(/saved · draft v/)).toBeVisible();
  expect(savedBody).not.toBeNull();
  expect(savedBody!.nodes).toHaveLength(2);
});

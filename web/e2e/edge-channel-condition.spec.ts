import { test, expect } from "@playwright/test";
import { installApiMocks, seedFakeSession, FAKE_FLOW_ID, FAKE_TENANT_ID } from "./mocks";

/**
 * migration 110 / the "user-friendly UI" milestone — branching a flow on
 * `case.channel` (which platform a case arrived from) without typing an
 * expression by hand.
 *
 * Rewritten for the row-based condition builder (ConditionBuilder.tsx) that
 * replaced the raw-textarea-plus-quick-insert-dropdowns as the default edge
 * condition UI. The dropdown-driven flow this test covered originally is
 * now the builder's field/operator/value pickers, not the "Advanced" raw
 * box — and the old quick-insert joiner used `&&`, which the backend
 * evaluator (interpreter/conditions.py, a Python AST eval) never actually
 * accepted; joins are `and` now, both in the builder's serialization and in
 * the Advanced box's manual quick-insert buttons.
 */

test.beforeEach(async ({ page }) => {
  await seedFakeSession(page);
});

test("branch on case.channel via the row builder's field/value pickers", async ({ page }) => {
  await installApiMocks(page);

  // two nodes + one UNCONDITIONAL edge, so there's something to click and
  // no label rendered yet to interfere.
  const flow = {
    flow_id: FAKE_FLOW_ID, tenant_id: FAKE_TENANT_ID, team: "support",
    name: "E2E Test Flow", status: "draft", version: 1, published_version: null,
    sf_entry: false,
    nodes: [
      { node_id: "n1", type: "identify", label: null, position_x: 100, position_y: 100, config: {} },
      { node_id: "n2", type: "sf_case", label: null, position_x: 400, position_y: 100, config: {} },
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
  const flowItem = page.locator(".flow-item", { hasText: "E2E Test Flow" });
  await expect(flowItem).toBeVisible();
  await flowItem.click();

  // the editor now lands on the chat view by default (AI-edit as the front
  // door) — switch to the graph to reach the canvas at all
  await page.getByRole("button", { name: "🗺️ Graph" }).click();

  await expect(page.getByText("identify", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("sf_case", { exact: true }).first()).toBeVisible();

  // click the edge between the two nodes (React Flow's edge interaction path)
  await page.locator(".react-flow__edge").first().click({ force: true });

  // the edge inspector opened — tick "conditional"; the row builder (not a
  // raw textarea) is the default view for a simple AND-only condition
  const conditionalBox = page.getByRole("checkbox", { name: "conditional" });
  await expect(conditionalBox).toBeVisible();
  await conditionalBox.check();

  const builder = page.locator(".condbuilder");
  await expect(builder).toBeVisible();
  const firstRow = builder.locator(".condbuilder__row").first();

  // checking "conditional" seeds a default row (tier equals enterprise)
  await expect(firstRow.locator("select").first()).toHaveValue("tier");

  // switch the field to "Case channel" and pick HubSpot from its value dropdown
  await firstRow.locator("select").first().selectOption("case.channel");
  const valueSelect = firstRow.locator("select").nth(2); // 0: field, 1: operator, 2: value
  await valueSelect.selectOption("hubspot");

  const advancedToggle = page.getByRole("button", { name: /Advanced/ });
  await advancedToggle.click();
  const exprBox = page.locator("textarea");
  await expect(exprBox).toHaveValue("case.channel == 'hubspot'");

  // the manual "and"/"or" quick-insert buttons exist and are correctly
  // labeled — never "&&"/"||", which the backend's Python-AST condition
  // evaluator can't parse
  await expect(page.getByRole("button", { name: "and", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "or", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "&&", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "||", exact: true })).toHaveCount(0);

  // the routed_team quick-insert dropdown auto-joins onto existing text
  // with "and" on its own (no manual button click needed)
  const routedTeamInsert = page.locator("select").filter({ hasText: "routed_team" });
  await routedTeamInsert.selectOption("support");
  await expect(exprBox).toHaveValue("case.channel == 'hubspot' and routed_team == 'support'");
});

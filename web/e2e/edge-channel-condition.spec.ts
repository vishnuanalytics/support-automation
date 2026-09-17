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

test("repeat-picking a quick-insert option doesn't duplicate it, and back-to-back picks still join with 'and'", async ({ page }) => {
  // Regression test (2026-09-17): the quick-insert selects always reset to
  // their placeholder after a pick, so re-selecting the *same* option still
  // fires onChange (value goes "" -> that option again) — `insert()` used
  // to blindly append on every call, so clicking "email" three times built
  // "case.channel == 'email' and case.channel == 'email' and case.channel
  // == 'email'". Fixed by `insertClause` skipping an insert whose exact
  // snippet already appears in the expression.
  //
  // Verifying that surfaced a second, related bug in the same function:
  // `insert()` re-focuses the textarea at the end (so the user can keep
  // typing) — but that means a *second* quick-insert click right after the
  // first one sees the textarea as "focused" and wrongly skipped the
  // "and"-joiner logic (which was gated on `!focused`), concatenating two
  // real clauses with no separator at all: "...'email'case.channel ==
  // 'hubspot'" — not even valid syntax. Fixed by making the join check
  // purely textual (does the text before the cursor need a joiner),
  // independent of focus state.
  await installApiMocks(page);
  const flow = {
    flow_id: FAKE_FLOW_ID, tenant_id: FAKE_TENANT_ID, team: "support",
    name: "E2E Test Flow", status: "draft", version: 1, published_version: null,
    sf_entry: false,
    nodes: [
      { node_id: "n1", type: "policy_gate", label: "Evaluate rules", position_x: 100, position_y: 100, config: {} },
      { node_id: "n2", type: "task_dispatch", label: "Raise the task", position_x: 450, position_y: 100, config: {} },
    ],
    // an "or" condition lands directly in Advanced mode (too complex for
    // the row builder), exactly where the quick-insert dropdowns live
    edges: [{ edge_id: "e1", source_node_id: "n1", target_node_id: "n2", condition: { if: "tier == 'enterprise' or region == 'us'" } }],
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
  await page.getByText("Evaluate rules", { exact: true }).waitFor();
  await page.locator(".react-flow__edge").first().click({ force: true });

  const exprBox = page.locator("textarea");
  await expect(exprBox).toHaveValue("tier == 'enterprise' or region == 'us'");

  const channelSelect = page.locator("select").filter({ hasText: "channel" });
  await channelSelect.selectOption("email");
  await channelSelect.selectOption("email");
  await channelSelect.selectOption("email");
  await expect(exprBox).toHaveValue("tier == 'enterprise' or region == 'us' and case.channel == 'email'");

  // a genuinely different value right after still inserts, correctly joined
  await channelSelect.selectOption("hubspot");
  await expect(exprBox).toHaveValue(
    "tier == 'enterprise' or region == 'us' and case.channel == 'email' and case.channel == 'hubspot'",
  );
});

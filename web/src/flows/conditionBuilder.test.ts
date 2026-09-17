import { describe, expect, it } from "vitest";
import { parseCondition, serializeCondition } from "./conditionBuilder";

describe("parseCondition", () => {
  it("parses the exact expression that motivated this feature", () => {
    // policy.task != None && ... was never valid — the backend evaluator
    // (interpreter/conditions.py) parses `if` as Python, where the
    // equivalent join is `and`, not `&&`.
    const clauses = parseCondition("policy.task != None and routed_team == 'support' and case.channel == 'email'");
    expect(clauses).toEqual([
      { field: "policy.task", op: "is_set", value: "" },
      { field: "routed_team", op: "eq", value: "support" },
      { field: "case.channel", op: "eq", value: "email" },
    ]);
  });

  it("maps == None / != None to the is_set/is_not_set aliases", () => {
    expect(parseCondition("policy.task == None")).toEqual([{ field: "policy.task", op: "is_not_set", value: "" }]);
    expect(parseCondition("policy.task != None")).toEqual([{ field: "policy.task", op: "is_set", value: "" }]);
  });

  it("parses numbers and in/not-in lists", () => {
    expect(parseCondition("confidence >= 0.8")).toEqual([{ field: "confidence", op: "gte", value: "0.8" }]);
    expect(parseCondition("routed_team in ('support', 'sales')")).toEqual([
      { field: "routed_team", op: "in", value: "support, sales" },
    ]);
    expect(parseCondition("routed_team not in ['support']")).toEqual([
      { field: "routed_team", op: "not_in", value: "support" },
    ]);
  });

  it("returns [] for an empty expression and null for anything with or/not", () => {
    expect(parseCondition("")).toEqual([]);
    expect(parseCondition("tier == 'enterprise' or region == 'us'")).toBeNull();
    expect(parseCondition("not confidence_gate.pass")).toBeNull();
  });

  it("doesn't split ' and ' inside a quoted string value", () => {
    expect(parseCondition("classification.topic == 'billing and refunds'")).toEqual([
      { field: "classification.topic", op: "eq", value: "billing and refunds" },
    ]);
  });
});

describe("serializeCondition", () => {
  it("round-trips through the exact scenario that motivated this feature", () => {
    const clauses = parseCondition(
      "policy.task != None and routed_team == 'support' and case.channel == 'email'",
    )!;
    const expr = serializeCondition(clauses);
    expect(expr).toBe("policy.task != None and routed_team == 'support' and case.channel == 'email'");
    // and it must re-parse the same way (a real round trip, not just string luck)
    expect(parseCondition(expr)).toEqual(clauses);
  });

  it("never emits && or || — only Python's and/or", () => {
    const expr = serializeCondition([
      { field: "a", op: "eq", value: "foo" },
      { field: "b", op: "eq", value: "bar" },
    ]);
    expect(expr).not.toMatch(/&&|\|\|/);
    expect(expr).toBe("a == 'foo' and b == 'bar'");
  });

  it("serializes is_set/is_not_set back to None comparisons", () => {
    expect(serializeCondition([{ field: "policy.task", op: "is_set", value: "" }])).toBe("policy.task != None");
    expect(serializeCondition([{ field: "policy.task", op: "is_not_set", value: "" }])).toBe("policy.task == None");
  });

  it("quotes non-numeric values and leaves numbers/bools bare", () => {
    expect(serializeCondition([{ field: "tier", op: "eq", value: "enterprise" }])).toBe("tier == 'enterprise'");
    expect(serializeCondition([{ field: "confidence", op: "gte", value: "0.8" }])).toBe("confidence >= 0.8");
  });

  it("doesn't silently turn a fresh, not-yet-filled eq row into is_not_set", () => {
    // a brand new row defaults to {field, op: "eq", value: ""} before the
    // user has picked a value — round-tripping that through serialize then
    // parse (what happens on every re-render) must not flip its operator.
    const fresh = [{ field: "routed_team", op: "eq" as const, value: "" }];
    const expr = serializeCondition(fresh);
    expect(expr).toBe("routed_team == ''");
    expect(parseCondition(expr)).toEqual(fresh);
  });

  it("serializes in/not_in as a bracket list, never a bare single-element tuple", () => {
    expect(serializeCondition([{ field: "routed_team", op: "in", value: "support" }])).toBe(
      "routed_team in ['support']",
    );
  });
});

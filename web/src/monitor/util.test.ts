import { describe, expect, it } from "vitest";
import { askClaudePrompt, checkTable, toCsv } from "./util";

describe("toCsv", () => {
  it("quotes commas, quotes and line breaks", () => {
    expect(toCsv([["a,b", 'say "hi"', "two\nlines"]])).toBe('"a,b","say ""hi""","two\nlines"');
  });

  it("neutralises cells a spreadsheet would run as a formula", () => {
    expect(toCsv([["=HYPERLINK(1)", "@x", "+1", "-1"]])).toBe("'=HYPERLINK(1),'@x,'+1,'-1");
  });

  it("leaves numbers alone and empties blank", () => {
    expect(toCsv([[-5, null, undefined]])).toBe("-5,,");
  });
});

describe("checkTable", () => {
  it("labels object rows with every key seen", () => {
    expect(checkTable([{ a: 1, b: "x" }, { a: 2, c: null }])).toEqual({
      head: ["a", "b", "c"], body: [["1", "x", ""], ["2", "", ""]],
    });
  });

  it("shows array rows without a header", () => {
    expect(checkTable([[1, null, { k: 1 }]])).toEqual({ head: [], body: [["1", "", '{"k":1}']] });
  });

  it("handles no rows", () => {
    expect(checkTable([])).toEqual({ head: [], body: [] });
  });
});

describe("askClaudePrompt", () => {
  it("names the case and the tools to use", () => {
    const p = askClaudePrompt("00000042");
    expect(p).toContain("case 00000042");
    expect(p).toContain("get_case");
    expect(p).toContain("save_check");
  });
});

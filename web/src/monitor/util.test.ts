import { describe, expect, it } from "vitest";
import { toCsv } from "./util";

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

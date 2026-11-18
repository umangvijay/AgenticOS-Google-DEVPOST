import { describe, it, expect } from "vitest";
import { safeSnippet } from "../src/lib/safe-snippet";

describe("safeSnippet", () => {
  it("returns empty string for null/undefined", () => {
    expect(safeSnippet(undefined)).toBe("");
    expect(safeSnippet(null)).toBe("");
  });

  it("passes strings through with truncation", () => {
    expect(safeSnippet("hello world")).toBe("hello world");
    expect(safeSnippet("abcdefghij", 4)).toBe("abcd");
  });

  it("pretty-prints objects and keeps primitives readable", () => {
    expect(safeSnippet({ status_code: 200 })).toContain("200");
    expect(safeSnippet({ data: null })).toContain("null");
    expect(safeSnippet(42)).toContain("42");
    expect(safeSnippet(false)).toContain("false");
  });

  it("survives circular references", () => {
    const obj: Record<string, unknown> = { a: 1 };
    obj.self = obj;
    expect(() => safeSnippet(obj)).not.toThrow();
  });

  it("survives BigInt and other non-JSON-serializable values", () => {
    expect(() => safeSnippet({ n: 1n })).not.toThrow();
  });
});

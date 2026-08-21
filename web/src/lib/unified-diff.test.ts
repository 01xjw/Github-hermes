import { describe, expect, it } from "vitest";
import { parseUnifiedDiff, unifiedLineKind } from "./unified-diff";

describe("parseUnifiedDiff", () => {
  it("aligns replacement blocks and preserves source line numbers", () => {
    const document = parseUnifiedDiff(
      [
        "diff --git a/example.py b/example.py",
        "--- a/example.py",
        "+++ b/example.py",
        "@@ -10,3 +10,4 @@",
        " stable",
        "-old one",
        "-old two",
        "+new one",
        "+new two",
        "+new three",
        " tail",
      ].join("\n"),
    );

    expect(document.headers).toHaveLength(3);
    expect(document.hunks).toHaveLength(1);
    expect(document.hunks[0].rows).toEqual([
      {
        kind: "content",
        left: { kind: "context", lineNumber: 10, text: "stable" },
        right: { kind: "context", lineNumber: 10, text: "stable" },
      },
      {
        kind: "content",
        left: { kind: "deletion", lineNumber: 11, text: "old one" },
        right: { kind: "addition", lineNumber: 11, text: "new one" },
      },
      {
        kind: "content",
        left: { kind: "deletion", lineNumber: 12, text: "old two" },
        right: { kind: "addition", lineNumber: 12, text: "new two" },
      },
      {
        kind: "content",
        left: null,
        right: { kind: "addition", lineNumber: 13, text: "new three" },
      },
      {
        kind: "content",
        left: { kind: "context", lineNumber: 13, text: "tail" },
        right: { kind: "context", lineNumber: 14, text: "tail" },
      },
    ]);
  });
});

describe("unifiedLineKind", () => {
  it("classifies Git headers before additions and deletions", () => {
    expect(unifiedLineKind("+++ b/example.py")).toBe("header");
    expect(unifiedLineKind("--- a/example.py")).toBe("header");
    expect(unifiedLineKind("+value")).toBe("addition");
    expect(unifiedLineKind("-value")).toBe("deletion");
  });
});

// The size limits on what goes to the advisor and what comes back, and what counts as reading.
import { describe, expect, it } from "vitest";
import { CODE_CUT_NOTE, limitCodeBlocks } from "../src/advice.js";
import { BRIEF_CUT_MARKER, approxTokens, buildBrief, cutBrief } from "../src/brief.js";
import { readsCode } from "../src/observe.js";
import { RoleMap } from "../src/redact.js";

describe("limitCodeBlocks", () => {
  it("cuts each long block, keeps short ones and inline code", () => {
    const text = ["Use `x()`:", "```", "1", "2", "3", "```", "and", "~~~cpp", "a", "~~~", "done"].join("\n");
    expect(limitCodeBlocks(text, 1)).toEqual({
      text: ["Use `x()`:", "```", "1", "```", CODE_CUT_NOTE, "and", "~~~cpp", "a", "~~~", "done"].join("\n"),
      removed: 2,
    });
    expect(limitCodeBlocks(text, 3)).toEqual({ text, removed: 0 });
    expect(limitCodeBlocks(text, null)).toEqual({ text, removed: 0 });
  });

  it("0 removes every block; an unclosed block runs to the end and gets closed", () => {
    const text = ["Try:", "```cpp", "a();", "```", "Then:", "```", "b();", "c();"].join("\n");
    expect(limitCodeBlocks(text, 0)).toEqual({ text: ["Try:", CODE_CUT_NOTE, "Then:", CODE_CUT_NOTE].join("\n"), removed: 3 });
    expect(limitCodeBlocks(text, 1).text).toBe(["Try:", "```cpp", "a();", "```", "Then:", "```", "b();", "```", CODE_CUT_NOTE].join("\n"));
  });
});

describe("cutBrief", () => {
  it("leaves a brief within the limit alone", () => {
    expect(cutBrief("short", 10)).toEqual({ text: "short", truncated: false });
  });

  it("cuts the longest section at a line boundary, keeping its heading and the other sections", () => {
    const issue = `Issue:\n${Array.from({ length: 50 }, (_, i) => `issue line ${i}`).join("\n")}`;
    const text = `Intro.\n\n${issue}\n\nTheir question:\nWhy?`;
    const { text: out, truncated } = cutBrief(text, 60);
    expect(truncated).toBe(true);
    expect(out.length).toBeLessThanOrEqual(240);
    expect(out).toMatch(/^Intro\.\n\nIssue:\nissue line 0\n/);
    expect(out).toContain(`\n${BRIEF_CUT_MARKER}\n\nTheir question:\nWhy?`);
    expect(out).not.toMatch(/issue line \d+[^\n]*\n[^\n]*issue l$/m); // no half lines
  });

  it("cuts several sections when one is not enough, and anything when nothing else works", () => {
    const big = (h: string) => `${h}:\n${"word ".repeat(100).trim()}`;
    const { text: out } = cutBrief([big("A"), big("B"), "Q:\nwhy"].join("\n\n"), 60);
    expect(out.length).toBeLessThanOrEqual(240);
    expect(out.match(new RegExp(BRIEF_CUT_MARKER.replace(/[[\]]/g, "\\$&"), "g"))).toHaveLength(2);
    expect(out).toContain("Q:\nwhy");
    const tiny = cutBrief("x".repeat(100), 12);
    expect(tiny.text.length).toBeLessThanOrEqual(48);
    expect(tiny.text).toContain(BRIEF_CUT_MARKER);
  });

  it("buildBrief cuts after the redaction sweep, so no cut name leaks", () => {
    const roles = new RoleMap();
    const task = `Fix \`parse_value()\`.\n\n${"`parse_value()` is called here. ".repeat(60)}`;
    const brief = buildBrief(
      "L1",
      "Issue:\n{{task_summary}}\n\nQuestion:\n{{question}}",
      { intervention: "consult", reason: "", turn: 0, question: "Why?" },
      { task, recent: [], lastFailure: null, failedTests: [], lastEdit: null },
      roles,
      () => null,
      100,
    );
    expect(brief.truncated).toBe(true);
    expect(brief.tokens).toBe(approxTokens(brief.text));
    expect(brief.tokens).toBeLessThanOrEqual(100);
    expect(brief.text).not.toMatch(/parse|_value/);
    expect(brief.text).toContain("Question:\nWhy?");
  });
});

describe("readsCode", () => {
  const bash = (command: string) => ({ name: "bash", args: { command } });
  it("read-type tools and looking shell commands", () => {
    for (const obs of [
      { name: "read", args: { path: "a" } },
      { name: "grep", args: {} },
      { name: "ls", args: {} },
      bash("cat src/a.cpp"),
      bash("cd /testbed && grep -rn foo ."),
      bash("git log -3 --oneline"),
      bash("sed -n 1,20p a.cpp"),
    ]) {
      expect(readsCode(obs), JSON.stringify(obs)).toBe(true);
    }
  });
  it("not the tests, edits, builds, or writing git commands", () => {
    for (const obs of [
      { name: "edit", args: { path: "a" } },
      bash("/opt/lso/run-tests | tail"),
      bash("sed -i s/a/b/ a.cpp"),
      bash("make -j4"),
      bash("git apply x.patch"),
      bash("git commit -am x"),
    ]) {
      expect(readsCode(obs), JSON.stringify(obs)).toBe(false);
    }
  });
});

import { describe, expect, it } from "vitest";
import type { CompletionRequest } from "../src/client.js";
import { RoleMap } from "../src/redact.js";
import { DIFF_CUT, buildReviewBrief, cutDiff, parseRanking, type ReviewRequest, review } from "../src/review.js";

const DIFF = [
  "diff --git a/include/fmt/format.h b/include/fmt/format.h",
  "index 1a2b3c4..5d6e7f8 100644",
  "--- a/include/fmt/format.h",
  "+++ b/include/fmt/format.h",
  "@@ -10,3 +10,3 @@ void write_padded(buffer& out) {",
  "-  if (spec_width > 0) emit_fill(out);",
  "+  while (spec_width > 0) emit_fill(out);",
  "   return;",
].join("\n");

const req = (over: Partial<ReviewRequest> = {}): ReviewRequest => ({
  level: "L3",
  task: "fmtlib__fmt-1234",
  issue: "`write_padded` pads only once in fmt.",
  candidates: [
    { label: "A", diff: DIFF, builds: true, broken: [] },
    { label: "B", diff: DIFF.replace("while", "for (;"), builds: false, broken: ["format-test", "core-test"] },
  ],
  templates: { system: "Review {{count}} candidates ({{labels}}) at {{level}}.", brief: "Issue:\n{{issue}}\n\n{{candidates}}" },
  maxDiffTokens: 6000,
  maxAnswerTokens: null,
  ...over,
});

describe("review brief", () => {
  it("shows each candidate's diff and local check verbatim at L3", () => {
    const { system, brief } = buildReviewBrief(req(), new RoleMap());
    expect(system).toBe("Review 2 candidates (A, B) at L3.");
    expect(brief).toContain("Candidate A\nBuilds: yes\nVisible tests broken against the base commit: none\n```diff\n" + DIFF);
    expect(brief).toContain("Candidate B\nBuilds: no\nVisible tests broken against the base commit: 2 (format-test, core-test)");
  });

  it("redacts paths, names and the project at L2, and drops index lines", () => {
    const roles = new RoleMap();
    const { brief, identifiersRedacted } = buildReviewBrief(req({ level: "L2" }), roles);
    for (const raw of ["format.h", "write_padded", "spec_width", "emit_fill", "fmt", "1a2b3c4", "format-test"]) {
      expect(brief).not.toContain(raw);
    }
    expect(brief).toMatch(/^\+ {2}while \(<variable_\d+> > 0\) <function_\d+>\(<variable_\d+>\);$/m);
    expect(brief).toMatch(/^diff --git a\/<file_\d+> b\/<file_\d+>$/m);
    expect(identifiersRedacted).toBeGreaterThan(5);
  });

  it("refuses L0 and L1: a diff is code", () => {
    expect(() => buildReviewBrief(req({ level: "L1" }), new RoleMap())).toThrow(/L2 or L3/);
  });

  it("cuts a long diff at a line, saying how much is left", () => {
    const long = Array.from({ length: 100 }, (_, i) => `+line ${i}`).join("\n");
    const cut = cutDiff(long, 10);
    expect(cut.split("\n").length).toBeLessThan(10);
    expect(cut).toMatch(/\[diff cut: \d+ more lines\]$/);
    expect(cut.endsWith(DIFF_CUT(100 - cut.split("\n").length + 1))).toBe(true);
    expect(cutDiff("+a\n", 10)).toBe("+a");
  });
});

describe("ranking", () => {
  it("reads the last RANKING line, known labels only, each once", () => {
    expect(parseRanking("A looks off.\nRANKING: B > A > C", ["A", "B", "C"])).toEqual(["B", "A", "C"]);
    expect(parseRanking("**RANKING:** Candidate c, a, c, Z", ["A", "B", "C"])).toEqual(["C", "A"]);
    expect(parseRanking("RANKING: A\nthen\nRANKING: B > A", ["A", "B"])).toEqual(["B", "A"]);
    expect(parseRanking("I prefer B.", ["A", "B"])).toEqual([]);
  });

  it("asks the advisor once and picks the first ranked; an error picks nothing", async () => {
    const sent: CompletionRequest[] = [];
    const ok = {
      complete: async (r: CompletionRequest) => {
        sent.push(r);
        return { text: "B breaks tests.\nRANKING: A > B", outputTokens: 9, cachedTokens: 0, promptTokens: 100, reasoningTokens: 3, finishReason: "stop", latencyMs: 5 };
      },
    };
    const got = await review(req({ requestId: "review-1" }), ok);
    expect(sent).toHaveLength(1);
    expect(sent[0]!.requestId).toBe("review-1");
    expect(got.pick).toBe("A");
    expect(got.completion?.promptTokens).toBe(100);
    const failed = await review(req(), { complete: async () => Promise.reject(new Error("HTTP 429")) });
    expect(failed).toMatchObject({ pick: null, answer: null, error: "HTTP 429", ranking: [] });
  });
});

import { describe, expect, it } from "vitest";
import { type BriefContext, approxTokens, buildBrief } from "../src/brief.js";
import { RoleMap } from "../src/redact.js";
import { promptSet } from "./helpers.js";

const TASK = [
  "`json::dump()` crashes on a NaN inside a JsonArray.",
  "```cpp",
  "auto j = json::parse(input);",
  "j.dump(indent_width);",
  "```",
  "It should throw out_of_range instead.",
].join("\n");

const SOURCE = Array.from({ length: 30 }, (_, i) => `line_${i + 1}();`).join("\n");

const failure = {
  name: "bash",
  args: { command: "/opt/lso/run-tests 2>&1 | tail" },
  result:
    "src/serializer.hpp:12:5: error: no member named 'dump_float' in 'Serializer'\n" +
    "50% tests passed, 1 tests failed out of 2\nThe following tests FAILED:\n\t  4 - test-dump_nan (Failed)\n",
  isError: true,
};

const ctx: BriefContext = {
  task: TASK,
  recent: [
    { name: "read", args: { path: "src/serializer.hpp" }, result: "...", isError: false },
    { name: "bash", args: { command: "grep -n dump_float src/serializer.hpp" }, result: "", isError: true },
    failure,
  ],
  lastFailure: failure,
  failedTests: ["test-dump_nan"],
  lastEdit: null,
};

const TEMPLATE =
  "TASK {{task_summary}}\nQ {{question}}\nTRIED {{tried}}\nHYP {{hypothesis}}\nERR {{error}}\nCODE {{code}}\nEND {{trigger}} {{level}}";
const req = {
  intervention: "consult" as const,
  reason: "consult tool",
  turn: 3,
  question: "Why is `Serializer::dump_float` missing?",
  tried: "I grepped for dump_float in serializer.hpp.",
  hypothesis: "JsonArray never calls dump_float.",
};
const stuck = { intervention: "stuck" as const, reason: "repeat_calls", turn: 3 };
const read = (path: string) => (path === "src/serializer.hpp" ? SOURCE : null);
const build = (level: "L0" | "L1" | "L2" | "L3", roles = new RoleMap()) => buildBrief(level, TEMPLATE, req, ctx, roles, read);

// Every identifier in the fixtures; none may appear below L3.
const IDENTIFIERS = ["json::dump", "JsonArray", "out_of_range", "Serializer", "dump_float", "serializer.hpp", "indent_width", "line_12"];

describe("brief levels", () => {
  it("L0: words only; errors as a category, no code, no test names, no identifiers", () => {
    const b = build("L0");
    for (const id of IDENTIFIERS) expect(b.text).not.toContain(id);
    expect(b.text).not.toContain("auto j");
    expect(b.text).toContain("[code omitted]");
    expect(b.text).toContain("ERR 1 test(s) fail.");
    expect(b.text).not.toContain("test-dump_nan");
    expect(b.text).toMatch(/TRIED I grepped for <variable_\d+> in <file_\d+>\./);
    expect(b.text).toContain("HYP <type_");
    expect(b.text).toMatch(/CODE\s*\nEND consult L0$/);
    expect(b.identifiersRedacted).toBeGreaterThan(0);
  });

  it("L1: adds the error message and test names, still redacted", () => {
    const b = build("L1");
    for (const id of IDENTIFIERS) expect(b.text).not.toContain(id);
    expect(b.text).toContain("error: no member named '<variable_");
    expect(b.text).toContain("in '<type_");
    expect(b.text).toMatch(/4 - test-<variable_\d+> \(Failed\)/);
    expect(b.text).not.toContain("line_");
  });

  it("L2: adds redacted code excerpts around the error location", () => {
    const b = build("L2");
    for (const id of IDENTIFIERS) expect(b.text).not.toContain(id);
    expect(b.text).toMatch(/CODE <file_\d+>:12\n {1}9 \| <function_\d+>\(\);/);
    expect(b.text).toContain("15 | <function_");
    expect(b.text).not.toContain(" 8 |");
    expect(b.text).toContain("```cpp\nauto j = <namespace_1>::<function_2>(<variable_1>);"); // the task's code, redacted
  });

  it("L3: everything verbatim, nothing redacted", () => {
    const b = build("L3");
    expect(b.identifiersRedacted).toBe(0);
    expect(b.text).toContain("auto j = json::parse(input);");
    expect(b.text).toContain("src/serializer.hpp:12\n 9 | line_9();");
    expect(b.text).toContain("4 - test-dump_nan (Failed)");
    expect(b.text).toContain("Q Why is `Serializer::dump_float` missing?");
  });

  it("harness triggers: a fixed question, recent actions as `tried`, no hypothesis", () => {
    const b = buildBrief("L0", TEMPLATE, stuck, ctx, new RoleMap(), read);
    expect(b.text).toContain("Q I seem to be stuck. What should I try next?");
    expect(b.text).toContain("TRIED - read <file_1>\n- ran a shell command (failed)\n- ran the tests (failing)");
    expect(b.text).toContain("HYP \nERR");
    expect(b.text).toMatch(/END stuck L0$/);
    const l2 = buildBrief("L2", "{{tried}}", stuck, ctx, new RoleMap(), read);
    expect(l2.text).toBe("- read <file_1>\n- ran `grep -n <variable_1> <file_1>` (failed)\n- ran the tests (failing)");
  });

  it("the role map is shared, so placeholders are stable across briefs", () => {
    const roles = new RoleMap();
    const first = build("L1", roles);
    const size = roles.size;
    const second = build("L1", roles);
    expect(second.text).toBe(first.text);
    expect(roles.size).toBe(size);
  });

  it("only the placeholders a template uses are computed (and enter the role map)", () => {
    const roles = new RoleMap();
    buildBrief("L1", "{{question}}", { ...req, question: "plain words" }, ctx, roles, read);
    expect(roles.size).toBe(0);
  });

  it("token counts are approximate", () => {
    const b = build("L2");
    expect(b.tokens).toBe(approxTokens(b.text));
  });

  it("a turn-0 plan brief from the default template has no empty sections", () => {
    const empty: BriefContext = { task: "Make `parse()` accept empty input.", recent: [], lastFailure: null, failedTests: [], lastEdit: null };
    const brief = buildBrief("L1", promptSet("default").texts.brief, { intervention: "plan", reason: "", turn: 0 }, empty, new RoleMap());
    expect(brief.text).not.toMatch(/What they have tried:|Latest build or test output:|Relevant code:/);
    expect(brief.text).toMatch(/Issue:\nMake <function_1>\(\) accept empty input\.\n\nTheir question:\n/);
  });

  it("the repository's brief templates render without leftover placeholders", () => {
    for (const name of ["default", "structured"]) {
      const b = buildBrief("L2", promptSet(name, true).texts.brief, req, ctx, new RoleMap(), read);
      expect(b.text).not.toMatch(/\{\{/);
      expect(b.text).toMatch(/Why is `<[a-z]+_\d+>::<variable_\d+>` missing\?/);
    }
  });

  it("falls back to the last edit when the error names no readable location", () => {
    const edited = { ...ctx, lastFailure: null, lastEdit: { path: "src/a.cpp", text: "int fix_me = 1;" } };
    expect(buildBrief("L2", "{{code}}", req, edited, new RoleMap()).text).toBe("<file_1> (last edit)\nint <variable_1> = 1;");
    expect(buildBrief("L3", "{{code}}", req, edited, new RoleMap()).text).toBe("src/a.cpp (last edit)\nint fix_me = 1;");
  });
});

describe("evidence, reasoning, edits", () => {
  const T = "EV {{evidence}}\nRS {{reasoning}}\nED {{edits}}";
  const long = Array.from({ length: 100 }, (_, i) => `out_${i}`).join("\n");
  const rich: BriefContext = {
    ...ctx,
    recent: [...ctx.recent, { name: "bash", args: { command: "cat src/serializer.hpp" }, result: long, isError: false }],
    thinking: "JsonArray skips dump_float, so the NaN reaches the stream.",
    edits: [{ path: "src/serializer.hpp", text: "void dump_float(double x);" }],
  };
  const make = (level: "L1" | "L2" | "L3") => buildBrief(level, T, stuck, rich, new RoleMap(), read).text;

  it("L3: tool results cut to head and tail, the latest thinking and edits verbatim", () => {
    const b = make("L3");
    expect(b).toContain("$ cat src/serializer.hpp\n```\nout_0\n");
    expect(b).toContain("[… 60 lines …]\nout_90");
    expect(b).toContain("$ grep -n dump_float src/serializer.hpp  (failed)");
    expect(b).toContain("RS JsonArray skips dump_float");
    expect(b).toContain("src/serializer.hpp:\n```\nvoid dump_float(double x);\n```");
  });

  it("redacts at L2 and drops evidence and edits below it", () => {
    const l2 = make("L2");
    for (const id of ["dump_float", "serializer.hpp", "JsonArray"]) expect(l2).not.toContain(id);
    expect(l2).toContain("[… 60 lines …]");
    const l1 = make("L1");
    expect(l1).toMatch(/^EV $/m);
    expect(l1).toMatch(/^ED$/m);
    expect(l1).not.toContain("```");
  });
});

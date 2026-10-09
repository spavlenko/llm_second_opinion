// The help-flow changes after smoke round 3: orient by distinct files, stuck progress and
// reverts, trigger_skipped, reserve_for_end, clarify, memory, surrogates.
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { AdvisorClientLike, CompletionRequest } from "../src/client.js";
import type { AdvisorSettings, Event, PromptSet } from "../src/contracts.js";
import { EventWriter } from "../src/events.js";
import { filesRead, type ToolObservation } from "../src/observe.js";
import { RoleMap } from "../src/redact.js";
import { AdvisorSession, parseClarify } from "../src/session.js";
import { renderTemplate } from "../src/template.js";
import { type Decision, TriggerEngine } from "../src/triggers.js";
import { promptSet, runConfig, settings, validateEvent } from "./helpers.js";

const bash = (command: string, result = "ok", isError = false): ToolObservation => ({ name: "bash", args: { command }, result, isError });
const read = (path: string): ToolObservation => ({ name: "read", args: { path }, result: "x", isError: false });
const edit = (oldText = "a", newText = "b", path = "a.cpp"): ToolObservation => ({
  name: "edit",
  args: { path, edits: [{ oldText, newText }] },
  result: "ok",
  isError: false,
});
const tests = (result: string, isError = true) => bash("/opt/lso/run-tests | tail", result, isError);
const FAIL_A = "a.cpp:3: error: boom\n50% tests passed, 1 tests failed out of 2\nThe following tests FAILED:\n\t1 - t_a (Failed)";
const FAIL_B = "b.cpp:9: error: other\n50% tests passed, 1 tests failed out of 2\nThe following tests FAILED:\n\t2 - t_b (Failed)";
const PASS = "100% tests passed, 0 tests failed out of 2";

const fired = (d: Decision | null) => (d?.kind === "fire" ? d.fire.intervention : (d?.kind ?? null));

function turns(engine: TriggerEngine, list: ToolObservation[][], from = 0, stopping = false) {
  return list.map((calls, i) => {
    engine.turnStart(from + i);
    for (const c of calls) engine.observe(c);
    return fired(engine.turnEnd(stopping));
  });
}

describe("filesRead", () => {
  it("names the files read tools and printing commands read", () => {
    expect(filesRead(read("./src/a.cpp"))).toEqual(["src/a.cpp"]);
    expect(filesRead(bash("cat src/a.cpp include/b.h"))).toEqual(["src/a.cpp", "include/b.h"]);
    expect(filesRead(bash("sed -n '10,40p' src/a.cpp"))).toEqual(["src/a.cpp"]);
    expect(filesRead(bash("grep -n 'parse(' src/a.cpp src/b.cpp 2>/dev/null"))).toEqual(["src/a.cpp", "src/b.cpp"]);
    expect(filesRead(bash("grep -rn -e foo.h src/x.cpp"))).toEqual(["src/x.cpp"]); // -e: the pattern
    expect(filesRead(bash("head -n 50 a.hpp | tail -n 5"))).toEqual(["a.hpp"]);
    expect(filesRead(bash("cd /testbed && cat a.cpp && grep -rn foo src/"))).toEqual(["a.cpp"]);
  });
  it("not directories, globs, patterns, tests or edits", () => {
    expect(filesRead(bash("grep -rn some_name.cpp src"))).toEqual([]); // the pattern, then a directory
    expect(filesRead(bash("ls src/*.cpp"))).toEqual([]);
    expect(filesRead(bash("sed -i s/a/b/ a.cpp"))).toEqual([]);
    expect(filesRead(bash("/opt/lso/run-tests | tail -n 20"))).toEqual([]);
    expect(filesRead({ name: "grep", args: { pattern: "x" } })).toEqual([]);
  });
});

describe("orient by distinct files", () => {
  it("rereading the same file does not count; three distinct files do, read together in one turn", () => {
    const engine = new TriggerEngine(settings({ interventions: ["orient"], orient_after: 3 }));
    expect(turns(engine, [[read("a.cpp"), bash("cat a.cpp"), bash("sed -n 1,9p ./a.cpp")], [read("b.cpp"), bash("ls")]])).toEqual([null, null]);
    engine.turnStart(2);
    engine.observe(bash("grep -n x c.h"));
    expect(engine.turnEnd()).toMatchObject({ kind: "fire", fire: { intervention: "orient", reason: "3 files read", turn: 2 } });
  });
});

describe("stuck: progress and repeats", () => {
  const stuck = { repeat_calls: 9, same_error: 3, no_diff_turns: 2 };

  it("a test run with a new outcome is progress: no no_diff_turns while verifying", () => {
    const engine = new TriggerEngine(settings({ interventions: ["stuck"], stuck }));
    expect(turns(engine, [[edit()], [tests(FAIL_A)], [read("x.cpp")], [tests(FAIL_B)], [read("y.cpp")], [read("z.cpp")]])).toEqual([
      null, null, null, null, null, "stuck",
    ]);
  });

  it("the same failure again is not progress and counts as the same error", () => {
    const engine = new TriggerEngine(settings({ interventions: ["stuck"], stuck: { ...stuck, no_diff_turns: 99 } }));
    expect(turns(engine, [[tests(FAIL_A)], [edit()], [tests(FAIL_A)], [edit("c", "d")], [tests(FAIL_A)]])).toEqual([null, null, null, null, "stuck"]);
  });

  it("a new distinct error resets the same-error count", () => {
    const engine = new TriggerEngine(settings({ interventions: ["stuck"], stuck: { ...stuck, no_diff_turns: 99 } }));
    const build = (n: number) => bash(`make ${n}`, n % 2 ? "x.cpp:1: error: one" : "y.cpp:2: error: two", true);
    expect(turns(engine, [[build(1)], [build(1)], [build(2)], [build(2)], [build(1)]])).toEqual([null, null, null, null, null]);
  });

  it("reverting its own edit is not a file edit for no_diff_turns", () => {
    const engine = new TriggerEngine(settings({ interventions: ["stuck"], stuck }));
    expect(turns(engine, [[edit("a", "b")], [edit("b", "a")], [bash("git checkout -- a.cpp")]])).toEqual([null, null, "stuck"]);
  });

  it("never after the latest test run passed: trigger_skipped tests_passed, counters reset", () => {
    const engine = new TriggerEngine(settings({ interventions: ["stuck"], stuck }));
    expect(turns(engine, [[edit()], [tests(PASS, false)], [read("a")], [read("b")]])).toEqual([null, null, null, null]);
    expect(engine.takeSkips()).toEqual([{ intervention: "stuck", reason: "tests_passed", turn: 3 }]);
    expect(turns(engine, [[tests(FAIL_A)], [read("c")], [read("d")]], 4)).toEqual([null, null, "stuck"]);
  });
});

describe("trigger_skipped and reserve_for_end", () => {
  it("cooldown: the trigger is skipped, the next one in order is not tried past the same block", () => {
    const engine = new TriggerEngine(settings({ interventions: ["on_test_failure", "consult"], cooldown_turns: 1 }));
    engine.turnStart(0);
    engine.requestConsult();
    expect(turns(engine, [[tests(FAIL_A)]], 1)).toEqual([null]);
    expect(engine.takeSkips()).toEqual([{ intervention: "on_test_failure", reason: "cooldown", turn: 1 }]);
  });

  it("budget: skipped with budget_exhausted reported once", () => {
    const engine = new TriggerEngine(settings({ interventions: ["on_test_failure"], max_consults: 1 }));
    expect(turns(engine, [[tests(FAIL_A)], [tests(FAIL_B)], [tests(FAIL_A)]])).toEqual(["fire", "exhausted", "exhausted"].map((k) => (k === "fire" ? "on_test_failure" : k)));
    expect(engine.takeSkips().map((s) => s.reason)).toEqual(["budget", "budget"]);
  });

  it("other triggers and the tool stop short of the reserve; before_done uses it", () => {
    const engine = new TriggerEngine(
      settings({ interventions: ["consult", "on_test_failure", "before_done"], max_consults: 3, reserve_for_end: 1 }),
    );
    expect(turns(engine, [[tests(FAIL_A)], [tests(FAIL_B)], [tests(FAIL_A)]])).toEqual(["on_test_failure", "on_test_failure", null]);
    expect(engine.takeSkips()).toEqual([{ intervention: "on_test_failure", reason: "reserved_for_end", turn: 2 }]);
    const refusal = engine.requestConsult();
    expect(refusal).toMatchObject({ kind: "refused", rule: "reserved_for_end" });
    expect((refusal as { message: string }).message).toBe(
      "The last advisor consult is kept for a final check before you finish (2 of 3 used). Continue on your own.",
    );
    engine.turnStart(3);
    engine.observe(edit());
    expect(fired(engine.turnEnd(true))).toBe("before_done");
    expect(engine.consultsUsed).toBe(3);
  });

  it("without before_done enabled there is no reserve", () => {
    const engine = new TriggerEngine(settings({ interventions: ["on_test_failure"], max_consults: 2, reserve_for_end: 1 }));
    expect(turns(engine, [[tests(FAIL_A)], [tests(FAIL_B)]])).toEqual(["on_test_failure", "on_test_failure"]);
  });

  it("orient blocked by the reserve is skipped for good", () => {
    const engine = new TriggerEngine(settings({ interventions: ["orient", "before_done"], max_consults: 1, reserve_for_end: 1 }));
    engine.turnStart(0);
    expect(engine.beforeEdit()).toBeNull();
    expect(engine.takeSkips()).toEqual([{ intervention: "orient", reason: "reserved_for_end", turn: 0 }]);
  });
});

// --- session-level: clarify, memory, surrogates ------------------------------------------------

const PROMPTS: PromptSet = {
  name: "test",
  hash: "feedfacefeedface",
  texts: {
    executor_guidance: "g",
    consult_tool: "t",
    brief: "{{task_summary}}\n\nE: {{error}}\nQ: {{question}}",
    advisor_system: "Advise.\n\n{{#clarify}}\nYou may ask once: FILE or TEST_OUTPUT.\n{{/clarify}}\n\nBe short.",
    advice_injection: "{{advice}}",
  },
};

class Client implements AdvisorClientLike {
  requests: CompletionRequest[] = [];
  constructor(private readonly answers: string[]) {}
  async complete(req: CompletionRequest) {
    this.requests.push(req);
    return { text: this.answers.shift() ?? "ok", outputTokens: 5, cachedTokens: 0, promptTokens: 9, reasoningTokens: null, finishReason: "stop", latencyMs: 1 };
  }
}

const FILE = ["#include <x>", "int parse_value(int n) {", "  return compute_total(n) - 1;", "}", ""].join("\n");

function setup(overrides: Partial<AdvisorSettings>, answers: string[]) {
  const path = join(mkdtempSync(join(tmpdir(), "helpflow-")), "events.jsonl");
  const writer = new EventWriter(path);
  const client = new Client(answers);
  const session = new AdvisorSession({
    config: runConfig({ interventions: ["consult"], ...overrides }, PROMPTS),
    emit: (e) => writer.emit(e),
    client,
    readFile: (p) => (p === "src/v.cpp" ? FILE : null),
  });
  session.setTask("Fix `parse_value()` in src/v.cpp for empty input.");
  const events = (): Event[] => readFileSync(path, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
  return { session, client, events };
}

const failing: ToolObservation = {
  name: "bash",
  args: { command: "/opt/lso/run-tests" },
  result: "src/v.cpp:3: error: parse_value failed\n1 tests failed out of 2",
  isError: true,
};

function valid(ev: Event[]) {
  for (const e of ev) expect(validateEvent(e), JSON.stringify(validateEvent.errors)).toBe(true);
}

describe("clarify", () => {
  it("parses only the fixed forms", () => {
    expect(parseClarify("FILE <file_1>:10-20")).toEqual({ kind: "file", path: "<file_1>", start: 10, end: 20 });
    expect(parseClarify(" `TEST_OUTPUT` ")).toEqual({ kind: "test_output" });
    expect(parseClarify("FILE a.cpp:1-2\nand more")).toBeNull();
    expect(parseClarify("Look at FILE a.cpp:1-2")).toBeNull();
  });

  it("the advisor system prompt documents it only when clarify is on", () => {
    expect(setup({}, []).session.advisorSystem()).toBe("Advise.\n\nBe short.");
    expect(setup({ clarify: true }, []).session.advisorSystem()).toBe("Advise.\n\nYou may ask once: FILE or TEST_OUTPUT.\n\nBe short.");
  });

  it("FILE: fetched, redacted at L2, sent as a follow-up; one consult, two metered calls", async () => {
    const { session, client, events } = setup({ level: "L2", clarify: true }, ["FILE <file_1>:2-30", "Check <function_2>."]);
    session.observe(failing);
    const { text } = await session.consultTool({ question: "Why does `parse_value()` fail?" });
    expect(text).toBe("Check compute_total.");
    const ev = events();
    valid(ev);
    expect(ev.map((e) => e.type)).toEqual([
      "policy_rendered", "consult_requested", "brief_built", "advisor_request",
      "advisor_response", "advisor_followup", "advisor_response",
    ]);
    const followup = ev.find((e) => e.type === "advisor_followup") as Extract<Event, { type: "advisor_followup" }>;
    expect(followup.requested).toBe("FILE <file_1>:2-30");
    expect(followup.sent_text).toContain("<file_1>:2-5\n2 | int <function_1>(int n) {\n3 |   return <function_2>(n) - 1;");
    expect(followup.sent_text).not.toMatch(/parse_value|compute_total|src\/v\.cpp/);
    expect(client.requests).toHaveLength(2);
    expect(client.requests[1]!.continuation).toEqual([
      { role: "assistant", content: "FILE <file_1>:2-30" },
      { role: "user", content: followup.sent_text },
    ]);
    expect(client.requests.map((r) => r.requestId)).toEqual(["r1", "r1"]);
    expect(session.engine.consultsUsed).toBe(1);
  });

  it("TEST_OUTPUT at L1 is the error lines, redacted; FILE below L2 is refused in words; one follow-up at most", async () => {
    const { session, client, events } = setup({ level: "L1", clarify: true }, ["TEST_OUTPUT", "TEST_OUTPUT"]);
    session.observe(failing);
    const { text } = await session.consultTool({ question: "Why?" });
    expect(text).toBe("TEST_OUTPUT"); // the second request is taken as the answer
    expect(client.requests).toHaveLength(2);
    const sent = (events().find((e) => e.type === "advisor_followup") as { sent_text: string }).sent_text;
    expect(sent).toContain("<file_1>:3: error: <function_1> failed");
    const l1 = setup({ level: "L1", clarify: true }, ["FILE src/v.cpp:1-3", "ok"]);
    await l1.session.consultTool({ question: "Why?" });
    expect((l1.events().find((e) => e.type === "advisor_followup") as { sent_text: string }).sent_text).toContain("Code is not shared at this level (L1).");
  });

  it("clarify off: a request-shaped answer is just the answer", async () => {
    const { session, client } = setup({ level: "L2" }, ["TEST_OUTPUT"]);
    await session.consultTool({ question: "Why?" });
    expect(client.requests).toHaveLength(1);
  });
});

describe("memory", () => {
  it("re-sends earlier briefs and answers as prior messages; brief_text is the new brief only", async () => {
    const { session, client, events } = setup({ level: "L2", memory: true }, ["first answer", "second answer"]);
    await session.consultTool({ question: "First?" });
    session.observe(failing);
    await session.consultTool({ question: "Second?" });
    const requests = events().filter((e) => e.type === "advisor_request") as Extract<Event, { type: "advisor_request" }>[];
    valid(events());
    expect(requests.map((r) => r.history_turns)).toEqual([0, 1]);
    expect(client.requests[1]!.history).toEqual([
      { role: "user", content: requests[0]!.brief_text },
      { role: "assistant", content: "first answer" },
    ]);
    expect(requests[1]!.brief_text).toBe(client.requests[1]!.user);
    expect(requests[1]!.brief_text).not.toContain("First?");
    expect(requests[1]!.input_tokens).toBeGreaterThan(requests[0]!.input_tokens);

    const off = setup({ level: "L2" }, ["a", "b"]);
    await off.session.consultTool({ question: "First?" });
    await off.session.consultTool({ question: "Second?" });
    expect(off.client.requests[1]!.history).toEqual([]);
  });

  it("an empty answer (reasoning ate the budget) is a failed consult and stays out of the history", async () => {
    const { session, client, events } = setup({ level: "L2", memory: true }, ["  ", "second answer"]);
    const first = await session.consultTool({ question: "First?" });
    expect(first.advice).toBeNull();
    expect(first.text).toMatch(/could not be reached/);
    expect(events().find((e) => e.type === "advisor_error")).toMatchObject({ request_id: "r1", status: null });
    session.observe(failing);
    await session.consultTool({ question: "Second?" });
    expect(client.requests[1]!.history).toEqual([]);
    valid(events());
  });
});

describe("surrogates", () => {
  it("plausible fake names, deterministic per run, mapped back before injection", async () => {
    const make = () => setup({ level: "L1", surrogates: true }, []);
    const a = make();
    a.session.observe(failing);
    const briefA = a.session.brief({ intervention: "consult", reason: "x", turn: 0, question: "Why does `parse_value()` fail?" });
    const b = make();
    b.session.observe(failing);
    const briefB = b.session.brief({ intervention: "consult", reason: "x", turn: 0, question: "Why does `parse_value()` fail?" });
    expect(briefA.text).toBe(briefB.text); // deterministic for the same run identity
    expect(briefA.text).not.toMatch(/<\w+_\d+>|parse_value|src\/v\.cpp/);
    const built = a.events().find((e) => e.type === "brief_built") as Extract<Event, { type: "brief_built" }>;
    const fn = Object.keys(built.role_map).find((k) => built.role_map[k] === "parse_value")!;
    const file = Object.keys(built.role_map).find((k) => built.role_map[k] === "src/v.cpp")!;
    expect(fn).toMatch(/^[a-z]+_[a-z]+_\d+$/);
    expect(file).toMatch(/^src\/module_\d+\.cpp$/);
    expect(briefA.text).toContain(fn);
    expect(built.identifiers_redacted).toBeGreaterThanOrEqual(3);
    expect(a.session.roles.restore(`Check ${fn}() in ${file}, or module_${file.match(/\d+/)![0]}.cpp.`)).toBe(
      "Check parse_value() in src/v.cpp, or v.cpp.",
    );
  });

  it("another run gets other names; L3 stays verbatim", () => {
    const names = (task: string) => {
      const roles = new RoleMap({ surrogates: true, seed: `e/H/${task}/0` });
      return ["f1", "f2", "f3", "f4"].map((n) => roles.placeholder(n, "function"));
    };
    expect(names("t1")).toEqual(names("t1"));
    expect(names("t1")).not.toEqual(names("t2"));
    const { session } = setup({ level: "L3", surrogates: true }, []);
    expect(session.roles.surrogates).toBe(false);
  });

  it("never collides with a real name the run has seen", () => {
    // Every candidate of the first draw is seen text: the map must step past it.
    const probe = new RoleMap({ surrogates: true, seed: "s" });
    const first = probe.placeholder("x_real", "variable");
    const roles = new RoleMap({ surrogates: true, seed: "s" });
    roles.noteText(`int ${first} = 0;`);
    const second = roles.placeholder("x_real", "variable");
    expect(second).not.toBe(first);
    expect(roles.restore(second)).toBe("x_real");
    const many = new RoleMap({ surrogates: true, seed: "s" });
    const out = Array.from({ length: 60 }, (_, i) => many.placeholder(`name_${i}`, "type"));
    expect(new Set(out).size).toBe(60);
  });

  it("the sweep leaves surrogates alone and catches raw names", () => {
    const roles = new RoleMap({ surrogates: true, seed: "s" });
    const p = roles.placeholder("SerializerImpl", "type");
    expect(roles.sweep(`${p} and SerializerImpl`).text).toBe(`${p} and ${p}`);
    expect(roles.mapFor(`${p} and ${p}`)).toEqual({ [p]: "SerializerImpl" });
    expect(roles.count(`${p} and ${p}`)).toBe(2);
  });
});

describe("conditional template sections", () => {
  it("kept without markers when set, dropped with one blank line when not", () => {
    const t = "A\n\n{{#x}}\nB {{y}}\n{{/x}}\n\nC";
    expect(renderTemplate(t, { x: "1", y: "z" })).toBe("A\n\nB z\n\nC");
    expect(renderTemplate(t, { x: "" })).toBe("A\n\nC");
    expect(renderTemplate("{{#x}}\nB\n{{/x}}\nC", {})).toBe("C");
  });

  it("the repository's advisor_system prompts render with and without clarify", () => {
    for (const name of ["default", "hints-only"]) {
      const texts = { ...promptSet("default").texts, ...promptSet(name, true).texts };
      for (const clarify of [false, true]) {
        const session = new AdvisorSession({
          config: runConfig({ clarify }, { name, hash: "h", texts }),
          emit: () => {},
          client: new Client([]),
        });
        const system = session.advisorSystem();
        expect(system).not.toMatch(/\{\{|\n\n\n/);
        expect(system.includes("TEST_OUTPUT")).toBe(clarify);
      }
    }
  });
});

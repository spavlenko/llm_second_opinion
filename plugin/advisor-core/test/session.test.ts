import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { type AdvisorClientLike, AdvisorClientError, type CompletionRequest } from "../src/client.js";
import type { AdvisorSettings, Event, PromptSet } from "../src/contracts.js";
import { EventWriter } from "../src/events.js";
import type { ToolObservation } from "../src/observe.js";
import { type AdviceRecord, AdvisorSession, type ConsultArgs, CLOSING_REPORT,
  REPORT_GATE, TRUNCATED_MARKER } from "../src/session.js";
import { CODE_CUT_NOTE } from "../src/advice.js";
import { BRIEF_CUT_MARKER, DEFAULT_QUESTIONS } from "../src/brief.js";
import { DEFAULT_RULES, LOOSE, promptSet, runConfig, validateEvent } from "./helpers.js";

const PROMPTS: PromptSet = {
  name: "test",
  hash: "feedfacefeedface",
  texts: {
    executor_guidance: "Consult at most {{max_consults}} times.",
    consult_tool: "Ask the advisor ({{max_consults}} max).",
    brief: "[{{trigger}} {{level}}] {{task_summary}}\n\nQ: {{question}}\nT: {{tried}}\nE: {{error}}",
    advisor_system: "Advise at {{level}}. Max {{max_answer_tokens}} tokens.",
    advice_injection: "{{advice}} ({{consults_left}} left)",
  },
};

class FakeClient implements AdvisorClientLike {
  requests: CompletionRequest[] = [];
  finishReason: string | null = "stop";
  constructor(private readonly answers: (string | Error)[]) {}
  async complete(req: CompletionRequest) {
    this.requests.push(req);
    const a = this.answers.shift() ?? "ok";
    if (a instanceof Error) throw a;
    return { text: a, outputTokens: 12, cachedTokens: 0, promptTokens: 30, reasoningTokens: 8, finishReason: this.finishReason, latencyMs: 5 };
  }
}

function setup(settings: Partial<AdvisorSettings>, answers: (string | Error)[] = []) {
  const path = join(mkdtempSync(join(tmpdir(), "session-")), "events.jsonl");
  const writer = new EventWriter(path);
  const client = new FakeClient(answers);
  const records: AdviceRecord[] = [];
  const session = new AdvisorSession({
    config: runConfig(settings, PROMPTS),
    emit: (e) => writer.emit(e),
    client,
    log: (r) => records.push(r),
  });
  session.setTask("Fix `parse_value()` for empty input.");
  const events = (): Event[] =>
    readFileSync(path, "utf8")
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l));
  return { session, client, records, events };
}

function expectValid(events: Event[]) {
  for (const e of events) expect(validateEvent(e), JSON.stringify(validateEvent.errors)).toBe(true);
}

const fail: ToolObservation = {
  name: "bash",
  args: { command: "/opt/lso/run-tests" },
  result: "src/v.cpp:3: error: parse_value failed\n1 tests failed out of 2",
  isError: true,
};

describe("advisor session", () => {
  it("consult tool: request, brief, exact text sent, advice mapped back and applied", async () => {
    const { session, client, records, events } = setup({ level: "L1", max_answer_tokens: 200 }, [
      "Look at <function_1> when the input is empty.",
    ]);
    session.turnStart(2);
    session.observe(fail);
    const { text, advice } = await session.consultTool({ question: "Why does `parse_value()` fail?" });
    expect(text).toBe("Look at parse_value when the input is empty. (4 left)");
    session.applied(advice!);

    const ev = events();
    expectValid(ev);
    expect(ev.map((e) => e.type)).toEqual([
      "policy_rendered",
      "consult_requested",
      "brief_built",
      "advisor_request",
      "advisor_response",
      "advice_applied",
    ]);
    const [policy, requested, brief, request, response, applied] = ev as any[];
    expect(policy).toMatchObject({
      prompt_hash: "feedfacefeedface",
      executor_guidance: "Consult at most 5 times.",
      consult_tool: "Ask the advisor (5 max).",
    });
    expect(requested).toMatchObject({ reason: "Why does `parse_value()` fail?", turn: 2 });
    expect(brief).toMatchObject({ level: "L1", identifiers_redacted: 4, role_map_size: 2 });
    expect(brief.role_map).toEqual({ "<function_1>": "parse_value", "<file_1>": "src/v.cpp" });
    expect(response).toMatchObject({ prompt_tokens: 30, reasoning_tokens: 8, output_tokens: 12, finish_reason: "stop" });
    expect(client.requests[0]!.requestId).toBe("r1");
    expect(request.brief_text).toBe(client.requests[0]!.user);
    expect(request.brief_text).not.toContain("parse_value");
    expect(request.brief_text).toContain("E: <file_1>:3: error: <function_1> failed");
    expect(request).toMatchObject({ request_id: "r1", prompt_hash: "feedfacefeedface" });
    expect(request.input_tokens).toBeGreaterThan(brief.tokens);
    expect(applied).toMatchObject({ request_id: "r1", turn: 2 });
    expect(client.requests[0]).toMatchObject({ maxTokens: 200 });
    expect(client.requests[0]!.system).toBe("Advise at L1. Max 200 tokens.");
    expect(request.brief_text).toMatch(/^\[consult L1\] /);
    expect(records[0]).toMatchObject({ request_id: "r1", advice: "Look at <function_1> when the input is empty." });
    expect(records[0]!.role_map).toEqual({ "<function_1>": "parse_value", "<file_1>": "src/v.cpp" });
  });

  it("an advisor error becomes advisor_error and a tool result, not an exception", async () => {
    const { session, events } = setup({}, [new Error("HTTP 503: overloaded")]);
    const { text, advice } = await session.consultTool({ question: "help" });
    expect(advice).toBeNull();
    expect(text).toMatch(/could not be reached/);
    const ev = events();
    expectValid(ev);
    expect(ev.map((e) => e.type)).toEqual(["policy_rendered", "consult_requested", "brief_built", "advisor_request", "advisor_error"]);
    expect(ev[4]).toMatchObject({ request_id: "r1", message: "HTTP 503: overloaded", status: null });
    expect((ev[4] as any).latency_ms).toBeGreaterThanOrEqual(0);
  });

  it("a truncated answer is still injected, with a marker; advice_text stays as received", async () => {
    const { session, client, records, events } = setup({}, ["Look at <function_1> and then"]);
    client.finishReason = "length";
    const { text } = await session.consultTool({ question: "Why does `parse_value()` fail?" });
    expect(text).toBe(`Look at parse_value and then\n${TRUNCATED_MARKER} (4 left)`);
    const ev = events();
    expectValid(ev);
    expect(ev.find((e) => e.type === "advisor_response")).toMatchObject({
      finish_reason: "length",
      advice_text: "Look at <function_1> and then",
    });
    expect(records[0]!.advice).toBe("Look at <function_1> and then");
  });

  it("advisor_error takes the status and latency from the client's error", async () => {
    const { session, events } = setup({}, [new AdvisorClientError("HTTP 429: slow down", 429, 812)]);
    await session.consultTool({ question: "help" });
    const ev = events();
    expectValid(ev);
    expect(ev.at(-1)).toMatchObject({ type: "advisor_error", status: 429, latency_ms: 812 });
  });

  it("brief_built.role_map holds only this brief's placeholders; the advice log gets the whole map", async () => {
    const { session, records, events } = setup({ level: "L1", interventions: ["consult"] }, ["a", "b"]);
    session.setTask("Fix `parse_value()`.");
    await session.consultTool({ question: "Is `dump_options` related?" });
    session.setTask("Something else.");
    await session.consultTool({ question: "help" });
    const briefs = events().filter((e) => e.type === "brief_built") as any[];
    expect(briefs[0].role_map).toEqual({ "<function_1>": "parse_value", "<variable_1>": "dump_options" });
    expect(briefs[1].role_map).toEqual({});
    expect(briefs[1].role_map_size).toBe(2);
    expect(records[1]!.role_map).toEqual({ "<function_1>": "parse_value", "<variable_1>": "dump_options" });
  });

  it("report_gate: edits are refused until the executor files a report, and not when no consult is left for one", async () => {
    const { session } = setup({ interventions: ["consult", "stuck"], report_gate: true, max_consults: 2, reserve_for_end: 0 });
    expect(session.reportGate()).toBe(REPORT_GATE);
    await session.consultTool({ question: "Is the cause in parse_value?", tried: "reproduced", hypothesis: "empty input" });
    expect(session.reportGate()).toBeNull();
    const off = setup({ interventions: ["consult"], report_gate: false });
    expect(off.session.reportGate()).toBeNull();
    const kept = setup({ interventions: ["consult", "before_done"], report_gate: true, max_consults: 1, reserve_for_end: 1 });
    expect(kept.session.reportGate()).toBeNull(); // the only consult is kept for before_done
  });

  it("closing_report: one send-back when the executor stops with unreported edits", async () => {
    const edit: ToolObservation = { name: "edit", args: { path: "src/v.cpp", edits: [{ newText: "return {};" }] }, result: "ok", isError: false };
    const { session } = setup({ interventions: ["consult"], closing_report: true, max_consults: 3, reserve_for_end: 0 });
    expect(session.closingReport(true)).toBeNull(); // nothing edited
    session.observe(edit);
    expect(session.closingReport(false)).toBeNull(); // not stopping
    expect(session.closingReport(true)).toBe(CLOSING_REPORT);
    expect(session.closingReport(true)).toBeNull(); // once
    const reported = setup({ interventions: ["consult"], closing_report: true, max_consults: 3, reserve_for_end: 0 });
    reported.session.observe(edit);
    await reported.session.consultTool({ question: "Is the fix complete?", tried: "changed v.cpp", hypothesis: "empty input" });
    expect(reported.session.closingReport(true)).toBeNull(); // reported since the edit
    const off = setup({ interventions: ["consult"], max_consults: 3 });
    off.session.observe(edit);
    expect(off.session.closingReport(true)).toBeNull();
  });

  it("policy_rendered comes once, first, with a null tool when consult is off", async () => {
    const { session, events } = setup({ interventions: ["on_test_failure"] }, ["fix it"]);
    session.renderPolicy();
    await session.atStart();
    session.turnStart(1);
    session.observe(fail);
    await session.atTurnEnd();
    const ev = events();
    expectValid(ev);
    expect(ev.filter((e) => e.type === "policy_rendered")).toHaveLength(1);
    expect(ev[0]).toMatchObject({ type: "policy_rendered", consult_tool: null, executor_guidance: "Consult at most 5 times." });
  });

  it("plan at the start, then harness triggers at turn ends", async () => {
    const { session, events } = setup({ interventions: ["plan", "on_test_failure"], level: "L3" }, ["plan it", "fix it"]);
    session.turnStart(0);
    expect((await session.atStart())?.text).toBe("plan it (4 left)");
    expect(await session.atTurnEnd()).toBeNull(); // cooldown 0 still blocks the consult's own turn
    session.turnStart(1);
    session.observe(fail);
    const advice = await session.atTurnEnd();
    expect(advice?.text).toBe("fix it (3 left)");
    const ev = events();
    expectValid(ev);
    expect(ev.filter((e) => e.type === "trigger_fired")).toMatchObject([
      { intervention: "plan", turn: 0 },
      { intervention: "on_test_failure", reason: "the test run failed", turn: 1 },
    ]);
    const request = ev.find((e) => e.type === "advisor_request" && e.request_id === "r2") as any;
    expect(request.brief_text).toContain("parse_value"); // L3 is verbatim
  });

  it("budget: consults stop at max_consults and budget_exhausted is emitted once", async () => {
    const { session, events } = setup({ max_consults: 1 }, ["a"]);
    await session.consultTool({ question: "q1" });
    const second = await session.consultTool({ question: "q2" });
    await session.consultTool({ question: "q3" });
    expect(second.text).toMatch(/No advisor consults left/);
    const ev = events();
    expectValid(ev);
    expect(ev.map((e) => e.type)).toEqual([
      "policy_rendered",
      "consult_requested",
      "brief_built",
      "advisor_request",
      "advisor_response",
      "consult_refused",
      "budget_exhausted",
      "consult_refused",
    ]);
    expect(ev[5]).toMatchObject({ reason: "max_consults" });
    expect(ev[6]).toMatchObject({ consults_used: 1, limit: 1 });
  });

  describe("consult rules", () => {
    const strict = { rules: DEFAULT_RULES };
    const good = {
      question: "Why does `parse_value()` fail?",
      tried: "Ran the tests and read the parser code.",
      hypothesis: "The empty case returns before setting the result.",
    };
    const read: ToolObservation = { name: "read", args: { path: "src/v.cpp" }, result: "code", isError: false };

    async function refused(session: AdvisorSession, args: ConsultArgs = good) {
      const { text, advice } = await session.consultTool(args);
      expect(advice).toBeNull();
      return text;
    }

    it("min_own_actions: no consult before the executor has done anything, nor right after one", async () => {
      const { session, client, events } = setup({ ...strict, rules: { ...DEFAULT_RULES, tool_cooldown_turns: 0 } }, ["a", "b"]);
      session.turnStart(0);
      expect(await refused(session)).toBe("Investigate first: run or read something yourself before consulting (0 of 1 tool call so far).");
      session.observe(read);
      expect((await session.consultTool(good)).advice).not.toBeNull();
      expect(await refused(session)).toMatch(/^Investigate first: run or read something since your last consult/);
      session.observe(read);
      expect((await session.consultTool(good)).advice).not.toBeNull();
      const ev = events();
      expectValid(ev);
      expect(ev.filter((e) => e.type === "consult_refused")).toMatchObject([
        { reason: "min_own_actions", turn: 0 },
        { reason: "min_own_actions", turn: 0 },
      ]);
      expect(ev.filter((e) => e.type === "consult_requested")).toHaveLength(2);
      expect(client.requests).toHaveLength(2);
      expect(session.engine.consultsUsed).toBe(2); // refusals use up nothing
    });

    it("tool_cooldown_turns: refused within that many turns of the last consult, harness ones included", async () => {
      const { session, events } = setup({ ...strict, interventions: ["plan", "consult"] }, ["plan", "a"]);
      session.turnStart(0);
      await session.atStart(); // a consult at turn 0
      for (const turn of [0, 1, 2]) {
        session.turnStart(turn);
        session.observe(read);
        expect(await refused(session)).toMatch(new RegExp(`^Too soon after the last consult: keep working on your own for ${3 - turn} more turns?,`));
      }
      session.turnStart(3);
      expect((await session.consultTool(good)).advice).not.toBeNull();
      const reasons = events().filter((e) => e.type === "consult_refused").map((e: any) => e.reason);
      expect(reasons).toEqual(["tool_cooldown_turns", "tool_cooldown_turns", "tool_cooldown_turns"]);
    });

    it("require_hypothesis: tried and hypothesis of at least 5 words each", async () => {
      const { session, events } = setup(strict, ["a"]);
      session.observe(read);
      const message = "Say what you tried and what you think the cause is: `tried` and `hypothesis` need at least 5 words each.";
      expect(await refused(session, { question: "q" })).toBe(message);
      expect(await refused(session, { ...good, hypothesis: "none yet" })).toBe(message);
      expect(await refused(session, { ...good, tried: "read the issue" })).toBe(message);
      expect((await session.consultTool(good)).advice).not.toBeNull();
      expect(events().filter((e) => e.type === "consult_refused").map((e: any) => e.reason)).toEqual([
        "require_hypothesis",
        "require_hypothesis",
        "require_hypothesis",
      ]);
    });

    it("loose rules refuse nothing", async () => {
      const { session } = setup({}, ["a", "b"]);
      expect((await session.consultTool({ question: "q" })).advice).not.toBeNull();
      expect((await session.consultTool({ question: "q" })).advice).not.toBeNull();
    });

    it("the budget is checked first, and a budget refusal is consult_refused too", async () => {
      const { session, events } = setup({ ...strict, max_consults: 0 });
      expect(await refused(session, { question: "q" })).toBe("No advisor consults left (0 used). Continue on your own.");
      expect(events().map((e) => e.type)).toEqual(["policy_rendered", "consult_refused", "budget_exhausted"]);
    });
  });

  describe("max_advice_code_lines", () => {
    const answer = ["Check the loop:", "```cpp", "a();", "b();", "c();", "```", "Then `d()` it."].join("\n");

    it("long code blocks are cut before injection; advice_text keeps the original", async () => {
      const { session, records, events } = setup({ rules: { ...LOOSE, max_advice_code_lines: 2 } }, [answer]);
      const { text, advice } = await session.consultTool({ question: "q" });
      expect(text).toBe(["Check the loop:", "```cpp", "a();", "b();", "```", CODE_CUT_NOTE, "Then `d()` it. (4 left)"].join("\n"));
      session.applied(advice!);
      const ev = events();
      expectValid(ev);
      expect(ev.find((e) => e.type === "advisor_response")).toMatchObject({ advice_text: answer });
      expect(ev.find((e) => e.type === "advice_applied")).toMatchObject({ code_lines_removed: 1, injected_text: text });
      expect(records[0]!.advice).toBe(answer);
    });

    it("0 removes code blocks, keeps inline code; null keeps everything", async () => {
      const none = setup({ rules: { ...LOOSE, max_advice_code_lines: 0 } }, [answer]);
      const r = await none.session.consultTool({ question: "q" });
      expect(r.text).toBe(["Check the loop:", CODE_CUT_NOTE, "Then `d()` it. (4 left)"].join("\n"));
      expect(r.advice!.codeLinesRemoved).toBe(3);
      const all = setup({}, [answer]);
      const s = await all.session.consultTool({ question: "q" });
      expect(s.text).toBe(`${answer} (4 left)`);
      expect(s.advice!.codeLinesRemoved).toBe(0);
    });
  });

  it("max_brief_tokens: a long brief is cut and brief_built says so; the question survives", async () => {
    const { session, client, events } = setup({ level: "L3", max_brief_tokens: 40 }, ["a"]);
    session.setTask(Array.from({ length: 40 }, (_, i) => `line ${i} of a long issue`).join("\n"));
    await session.consultTool({ question: "Where is the bug?" });
    const brief = events().find((e) => e.type === "brief_built") as any;
    expect(brief).toMatchObject({ truncated: true });
    expect(brief.tokens).toBeLessThanOrEqual(40);
    expect(client.requests[0]!.user).toContain(BRIEF_CUT_MARKER);
    expect(client.requests[0]!.user).toContain("Q: Where is the bug?");
    expect(client.requests[0]!.user).toMatch(/^\[consult L3\] line 0 of a long issue/);
  });

  it("answer_target_words and field_target_words reach the prompts; a null target drops its line", () => {
    const texts = {
      ...PROMPTS.texts,
      executor_guidance: "Fields: {{field_target_words}} words.",
      consult_tool: "Each field under {{field_target_words}} words.",
      advisor_system: "Advise.\nAnswer in at most this many words: {{answer_target_words}}",
    };
    const make = (s: Partial<AdvisorSettings>) =>
      new AdvisorSession({ config: runConfig(s, { ...PROMPTS, texts }), emit: () => {}, client: new FakeClient([]) });
    const session = make({ field_target_words: 60, answer_target_words: 120 });
    expect(session.executorGuidance()).toBe("Fields: 60 words.");
    expect(session.consultToolDescription()).toBe("Each field under 60 words.");
    expect(session.advisorSystem()).toBe("Advise.\nAnswer in at most this many words: 120");
    expect(make({ answer_target_words: null }).advisorSystem()).toBe("Advise.");
  });

  describe("orient and before_done", () => {
    const read = (path: string): ToolObservation => ({ name: "read", args: { path }, result: "code", isError: false });
    const grep: ToolObservation = { name: "bash", args: { command: "grep -n parse_value src/p.cpp" }, result: "3: parse_value", isError: false };
    const editObs: ToolObservation = { name: "edit", args: { path: "src/v.cpp", edits: [{ newText: "if (s.empty()) return {};" }] }, result: "ok", isError: false };
    const pass: ToolObservation = { name: "bash", args: { command: "/opt/lso/run-tests | tail" }, result: "100% tests passed, 0 tests failed out of 2", isError: false };

    it("orient fires once after orient_after distinct files read, with the executor's notes in the brief", async () => {
      const { session, client, events } = setup({ level: "L3", interventions: ["orient"], orient_after: 3 }, ["Look at the empty case."]);
      session.turnStart(0);
      session.observe(read("src/v.cpp"));
      session.note("The parser lives in src/v.cpp.");
      expect(await session.atTurnEnd()).toBeNull();
      session.turnStart(1);
      session.observe(grep);
      session.observe(read("src/v.h"));
      session.note("parse_value returns early when the input is empty.");
      const advice = await session.atTurnEnd();
      expect(advice?.text).toBe("Look at the empty case. (4 left)");
      session.turnStart(2);
      session.observe(read("src/w.cpp"));
      expect(await session.atTurnEnd()).toBeNull(); // once only
      expect(await session.beforeEdit()).toBeNull();
      const ev = events();
      expectValid(ev);
      expect(ev.filter((e) => e.type === "trigger_fired")).toMatchObject([{ intervention: "orient", reason: "3 files read", turn: 1 }]);
      const brief = client.requests[0]!.user;
      expect(brief).toContain("- read src/v.cpp\n- ran `grep -n parse_value src/p.cpp`\n- read src/v.h");
      expect(brief).toContain("Their notes so far:\nThe parser lives in src/v.cpp.\nparse_value returns early when the input is empty.");
      expect(brief).toContain(`Q: ${DEFAULT_QUESTIONS.orient}`);
    });

    it("orient fires before the first edit if the reads have not triggered it; never after an edit", async () => {
      const { session, events } = setup({ interventions: ["orient"], orient_after: 5 }, ["advice"]);
      session.turnStart(0);
      session.observe(read("a.cpp"));
      expect(session.isEdit("edit", { path: "a.cpp" })).toBe(true);
      expect(session.isEdit("bash", { command: "sed -i s/a/b/ a.cpp" })).toBe(true);
      expect(session.isEdit("bash", { command: "cat a.cpp" })).toBe(false);
      expect((await session.beforeEdit())?.text).toBe("advice (4 left)");
      expect(await session.beforeEdit()).toBeNull();
      expect(events().filter((e) => e.type === "trigger_fired")).toMatchObject([{ intervention: "orient", reason: "before the first edit" }]);

      const late = setup({ interventions: ["orient"], orient_after: 1 }, ["advice"]);
      late.session.turnStart(0);
      late.session.observe(editObs);
      late.session.observe(read("a.cpp"));
      expect(await late.session.atTurnEnd()).toBeNull();
      expect(await late.session.beforeEdit()).toBeNull();
      expect(late.events().filter((e) => e.type === "trigger_fired")).toEqual([]);
    });

    it("before_done fires once, when the executor stops after an edit; skipped when the tests passed since", async () => {
      const { session, client, events } = setup({ level: "L3", interventions: ["before_done"] }, ["Looks right.", "x"]);
      session.turnStart(0);
      expect(await session.atTurnEnd(undefined, true)).toBeNull(); // no edit yet
      session.turnStart(1);
      session.observe(fail);
      session.observe(editObs);
      session.observe(pass);
      expect(await session.atTurnEnd()).toBeNull(); // not stopping
      session.turnStart(2);
      expect(await session.atTurnEnd(undefined, true)).toBeNull(); // the tests passed after the edit
      session.turnStart(3);
      session.observe(editObs);
      expect((await session.atTurnEnd(undefined, true))?.text).toBe("Looks right. (4 left)");
      session.turnStart(4);
      expect(await session.atTurnEnd(undefined, true)).toBeNull(); // once only
      const ev = events();
      expectValid(ev);
      expect(ev.filter((e) => e.type === "trigger_skipped")).toMatchObject([{ intervention: "before_done", reason: "tests_passed", turn: 2 }]);
      expect(ev.filter((e) => e.type === "trigger_fired")).toMatchObject([{ intervention: "before_done", reason: "stopped after editing", turn: 3 }]);
      expect(client.requests[0]!.user).toContain("I have not run the tests since my last change.");
    });

    it("before_done says so when the tests fail or were not run after the change", async () => {
      const failing = setup({ level: "L3", interventions: ["before_done"] }, ["x"]);
      failing.session.observe(editObs);
      failing.session.observe(fail);
      await failing.session.atTurnEnd(undefined, true);
      expect(failing.client.requests[0]!.user).toContain("The last test run after my change failed");
      expect(failing.client.requests[0]!.user).toContain("E: src/v.cpp:3: error: parse_value failed");
      const untested = setup({ level: "L3", interventions: ["before_done"] }, ["x"]);
      untested.session.observe(pass);
      untested.session.observe(editObs);
      await untested.session.atTurnEnd(undefined, true);
      expect(untested.client.requests[0]!.user).toContain("I have not run the tests since my last change.");
    });
  });

  it("renders the executor's guidance and the tool description from the prompt set", () => {
    const { session } = setup({ max_consults: 3, level: "L0" });
    expect(session.executorGuidance()).toBe("Consult at most 3 times.");
    expect(session.consultToolDescription()).toBe("Ask the advisor (3 max).");
    expect(session.advisorSystem()).toBe("Advise at L0. Max unlimited tokens.");
  });

  it("renders every slot of the repository's prompt sets with no placeholder left", async () => {
    for (const name of ["default", "structured", "hints-only"]) {
      const texts = { ...promptSet("default").texts, ...promptSet(name, true).texts };
      const config = runConfig({ level: "L2", interventions: ["plan"] }, { name, hash: "h", texts });
      const client = new FakeClient(["advice"]);
      const session = new AdvisorSession({ config, emit: () => {}, client });
      session.setTask("Fix `parse_value()`.");
      const advice = await session.atStart();
      const rendered = [session.executorGuidance(), session.consultToolDescription(), client.requests[0]!.system, client.requests[0]!.user, advice!.text];
      for (const text of rendered) expect(text).not.toMatch(/\{\{/);
      expect(advice!.text).toContain("advice");
    }
  });
});

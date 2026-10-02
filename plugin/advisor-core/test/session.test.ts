import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { AdvisorClientLike, CompletionRequest } from "../src/client.js";
import type { AdvisorSettings, Event, PromptSet } from "../src/contracts.js";
import { EventWriter } from "../src/events.js";
import type { ToolObservation } from "../src/observe.js";
import { type AdviceRecord, AdvisorSession } from "../src/session.js";
import { promptSet, runConfig, validateEvent } from "./helpers.js";

const PROMPTS: PromptSet = {
  name: "test",
  hash: "feedfacefeedface",
  texts: {
    executor_guidance: "Consult at most {{max_consults}} times.",
    consult_tool: "Ask the advisor ({{max_consults}} max).",
    brief: "[{{trigger}} {{level}}] {{task_summary}}\nQ: {{question}}\nE: {{error}}",
    advisor_system: "Advise at {{level}}. Max {{max_answer_tokens}} tokens.",
    advice_injection: "{{advice}} ({{consults_left}} left)",
  },
};

class FakeClient implements AdvisorClientLike {
  requests: CompletionRequest[] = [];
  constructor(private readonly answers: (string | Error)[]) {}
  async complete(req: CompletionRequest) {
    this.requests.push(req);
    const a = this.answers.shift() ?? "ok";
    if (a instanceof Error) throw a;
    return { text: a, outputTokens: 12, cachedTokens: 0, latencyMs: 5 };
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
      "consult_requested",
      "brief_built",
      "advisor_request",
      "advisor_response",
      "advice_applied",
    ]);
    const [requested, brief, request, , applied] = ev as any[];
    expect(requested).toMatchObject({ reason: "Why does `parse_value()` fail?", turn: 2 });
    expect(brief).toMatchObject({ level: "L1", identifiers_redacted: 4, role_map_size: 2 });
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
  });

  it("an advisor error becomes advisor_error and a tool result, not an exception", async () => {
    const { session, events } = setup({}, [new Error("HTTP 503: overloaded")]);
    const { text, advice } = await session.consultTool({ question: "help" });
    expect(advice).toBeNull();
    expect(text).toMatch(/could not be reached/);
    const ev = events();
    expectValid(ev);
    expect(ev.map((e) => e.type)).toEqual(["consult_requested", "brief_built", "advisor_request", "advisor_error"]);
    expect(ev[3]).toMatchObject({ request_id: "r1", message: "HTTP 503: overloaded" });
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
      "consult_requested",
      "brief_built",
      "advisor_request",
      "advisor_response",
      "consult_requested",
      "budget_exhausted",
      "consult_requested",
    ]);
    expect(ev[5]).toMatchObject({ consults_used: 1, limit: 1 });
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

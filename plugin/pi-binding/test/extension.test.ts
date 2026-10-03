import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { type Server, createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Ajv2020 } from "ajv/dist/2020.js";
import { afterEach, describe, expect, it } from "vitest";
import advisorExtension, { ADVICE_MESSAGE, EDIT_HELD, taskText } from "../src/index.js";

const schema = JSON.parse(readFileSync(new URL("../../../schemas/event.schema.json", import.meta.url), "utf8"));
const validate = new Ajv2020({ strict: false }).compile(schema);

let server: Server | undefined;
afterEach(() => new Promise<void>((r) => (server ? server.close(() => r()) : r())));

/** An OpenAI-compatible server answering with the given texts in order. */
async function advisor(answers: string[]): Promise<{ url: string; bodies: any[] }> {
  const bodies: any[] = [];
  server = createServer((req, res) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => {
      bodies.push({ ...JSON.parse(data), requestId: req.headers["x-lso-request-id"] });
      res.writeHead(200, { "Content-Type": "application/json" });
      const content = answers.shift() ?? "no more answers";
      res.end(JSON.stringify({ choices: [{ message: { content } }], usage: { completion_tokens: 5 } }));
    });
  });
  await new Promise<void>((r) => server!.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`, bodies };
}

/** Just enough of pi's ExtensionAPI to drive the extension. */
function fakePi() {
  const handlers = new Map<string, ((event: any, ctx: any) => unknown)[]>();
  const tools: any[] = [];
  const pi = {
    on(name: string, handler: (event: any, ctx: any) => unknown) {
      handlers.set(name, [...(handlers.get(name) ?? []), handler]);
      return () => {};
    },
    registerTool(tool: unknown) {
      tools.push(tool);
    },
  } as unknown as ExtensionAPI;
  const fire = async (name: string, event: object) => {
    let result: unknown;
    for (const h of handlers.get(name) ?? []) result = await h({ type: name, ...event }, { signal: undefined });
    return result as any;
  };
  return { pi, tools, handlers, fire };
}

function writeConfig(dir: string, advisorUrl: string, interventions: string[], advisorSettings = true, extra: object = {}) {
  const endpoint = { base_url: "http://unreachable.invalid/v1", model: "m", reasoning_effort: null, api_key_env: null, headers: {}, header_env: {}, temperature: null, top_p: null, sampling_seed: null };
  const config = {
    schema_version: "1",
    run: { experiment: "e", arm: "H", task: "t", seed: 0, config_hash: "0123456789abcdef" },
    executor: endpoint,
    advisor_model: advisorSettings ? endpoint : null,
    advisor: advisorSettings
      ? {
          level: "L1",
          interventions,
          prompts: "p",
          max_consults: 5,
          max_answer_tokens: 100,
          answer_target_words: 50,
          max_brief_tokens: 3000,
          field_target_words: 40,
          orient_after: 3,
          rules: { min_own_actions: 0, tool_cooldown_turns: 0, require_hypothesis: false, max_advice_code_lines: null },
          cooldown_turns: 0,
          periodic_every: null,
          stuck: { repeat_calls: 3, same_error: 3, no_diff_turns: 8 },
          ...extra,
        }
      : null,
    prompts: {
      name: "p",
      hash: "abcdefabcdefabcd",
      texts: {
        executor_guidance: "GUIDANCE: {{max_consults}} consults.",
        consult_tool: "Ask for advice ({{max_consults}} max).",
        brief: "{{task_summary}} | {{question}} | {{error}}",
        advisor_system: "sys {{level}} {{max_answer_tokens}}",
        advice_injection: "ADVICE: {{advice}} ({{consults_left}} left)",
      },
    },
    events_path: join(dir, "events.jsonl"),
  };
  const path = join(dir, "advisor.json");
  writeFileSync(path, JSON.stringify(config));
  return { LSO_ADVISOR_CONFIG: path, LSO_ADVISOR_BASE_URL: advisorUrl, LSO_ADVICE_LOG: join(dir, "advice.jsonl") };
}

const PROMPT = "Resolve the issue below.\n\n<issue>\n`parse_value()` crashes on empty input.\n</issue>\n";

describe("pi extension", () => {
  it("plan, consult tool, and a harness trigger, end to end against an HTTP advisor", async () => {
    const dir = mkdtempSync(join(tmpdir(), "pi-ext-"));
    const { url, bodies } = await advisor(["Start with <function_1>.", "Check the empty case.", "Read the test."]);
    const env = writeConfig(dir, url, ["plan", "consult", "on_test_failure"]);
    const { pi, tools, fire } = fakePi();
    advisorExtension(pi, env);

    // Guidance into the system prompt, the plan review as a custom message.
    const options = { appendSystemPrompt: "" };
    const start = await fire("before_agent_start", { prompt: PROMPT, systemPrompt: "", systemPromptOptions: options });
    expect(options.appendSystemPrompt).toBe("GUIDANCE: 5 consults.");
    expect(start).toEqual({
      message: { customType: ADVICE_MESSAGE, content: "ADVICE: Start with parse_value. (4 left)", display: true },
    });
    expect(bodies[0].messages[1].content).toMatch(/^<function_1>\(\) crashes on empty input\. \| How should I/);
    expect(bodies[0].max_tokens).toBe(100);
    expect(bodies[0].messages[0].content).toBe("sys L1 100");

    // The consult tool returns the advice as its result.
    await fire("turn_start", { turnIndex: 0 });
    expect(tools.map((t) => t.name)).toEqual(["consult"]);
    expect(tools[0].description).toBe("Ask for advice (5 max).");
    const args = { question: "Where is the empty case handled?", tried: "read the parser", hypothesis: "none yet" };
    const result = await tools[0].execute("call_1", args, undefined);
    expect(result.content).toEqual([{ type: "text", text: "ADVICE: Check the empty case. (3 left)" }]);
    await fire("turn_end", { message: { stopReason: "toolUse" }, toolResults: [] });

    // A failing test run in the next turn: advice injected at the turn's end.
    await fire("turn_start", { turnIndex: 1 });
    await fire("tool_result", {
      toolName: "bash",
      input: { command: "/opt/lso/run-tests" },
      content: [{ type: "text", text: "1 tests failed out of 3" }],
      isError: true,
    });
    const end = await fire("turn_end", { message: { stopReason: "toolUse" }, toolResults: [] });
    expect(end).toEqual({
      entries: [{ type: "custom_message", customType: ADVICE_MESSAGE, content: "ADVICE: Read the test. (2 left)", display: true }],
      continue: true,
    });

    const events = readFileSync(env.LSO_ADVISOR_CONFIG.replace("advisor.json", "events.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((l) => JSON.parse(l));
    for (const e of events) expect(validate(e), JSON.stringify(validate.errors)).toBe(true);
    expect(events.map((e) => e.type)).toEqual([
      "policy_rendered",
      "trigger_fired", "brief_built", "advisor_request", "advisor_response", "advice_applied",
      "consult_requested", "brief_built", "advisor_request", "advisor_response", "advice_applied",
      "trigger_fired", "brief_built", "advisor_request", "advisor_response", "advice_applied",
    ]);
    expect(events.filter((e) => e.type === "advice_applied").map((e) => e.turn)).toEqual([0, 0, 1]);
    expect(events[0]).toMatchObject({
      prompt_hash: "abcdefabcdefabcd",
      executor_guidance: "GUIDANCE: 5 consults.",
      consult_tool: tools[0].description,
    });
    expect(options.appendSystemPrompt).toContain(events[0].executor_guidance);
    expect(bodies.map((b) => b.requestId)).toEqual(["r1", "r2", "r3"]);
    expect(events.find((e) => e.type === "brief_built").role_map).toEqual({ "<function_1>": "parse_value" });
    const log = readFileSync(env.LSO_ADVICE_LOG, "utf8").trim().split("\n").map((l) => JSON.parse(l));
    expect(log[0].role_map).toEqual({ "<function_1>": "parse_value" });
    expect(log.map((r) => r.injected)).toEqual([
      "ADVICE: Start with parse_value. (4 left)",
      "ADVICE: Check the empty case. (3 left)",
      "ADVICE: Read the test. (2 left)",
    ]);
  });

  const readEvents = (dir: string) =>
    readFileSync(join(dir, "events.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((l) => JSON.parse(l));

  it("orient holds the first edit for advice; before_done continues a stopping turn once", async () => {
    const dir = mkdtempSync(join(tmpdir(), "pi-ext-"));
    const { url, bodies } = await advisor(["Check the empty case first.", "```sh\na\nb\nc\n```\nLooks right."]);
    const env = writeConfig(dir, url, ["orient", "before_done"], true, {
      orient_after: 5,
      rules: { min_own_actions: 1, tool_cooldown_turns: 2, require_hypothesis: true, max_advice_code_lines: 1 },
    });
    const { pi, fire } = fakePi();
    advisorExtension(pi, env);
    await fire("before_agent_start", { prompt: PROMPT, systemPrompt: "", systemPromptOptions: { appendSystemPrompt: "" } });

    await fire("turn_start", { turnIndex: 0 });
    await fire("tool_result", { toolName: "read", input: { path: "math.sh" }, content: [{ type: "text", text: "add() { echo $(($1 - $2)); }" }], isError: false });
    const toolUse = { stopReason: "toolUse", content: [{ type: "text", text: "add subtracts." }, { type: "toolCall" }] };
    expect(await fire("turn_end", { message: toolUse, toolResults: [{}] })).toBeUndefined();

    // The first edit: held, with the advice as the reason; a read is not held.
    await fire("turn_start", { turnIndex: 1 });
    expect(await fire("tool_call", { toolName: "read", input: { path: "math.sh" } })).toBeUndefined();
    const held = await fire("tool_call", { toolName: "edit", input: { path: "math.sh", edits: [] } });
    expect(held).toEqual({ block: true, reason: `${EDIT_HELD}\n\nADVICE: Check the empty case first. (4 left)` });
    expect(bodies[0].messages[1].content).toContain("Am I looking in the right place");
    expect(await fire("tool_call", { toolName: "edit", input: { path: "math.sh", edits: [] } })).toBeUndefined();
    await fire("tool_result", { toolName: "edit", input: { path: "math.sh", edits: [] }, content: [{ type: "text", text: "ok" }], isError: false });
    expect(await fire("turn_end", { message: toolUse, toolResults: [{}] })).toBeUndefined();

    // The executor stops: before_done, with its code cut to max_advice_code_lines, and continue.
    await fire("turn_start", { turnIndex: 2 });
    const done = { stopReason: "stop", content: [{ type: "text", text: "Fixed." }] };
    const end = await fire("turn_end", { message: done, toolResults: [] });
    expect(end).toEqual({
      entries: [
        {
          type: "custom_message",
          customType: ADVICE_MESSAGE,
          content: "ADVICE: ```sh\na\n```\n[code shortened: the advisor gives hints, you write the fix]\nLooks right. (3 left)",
          display: true,
        },
      ],
      continue: true,
    });
    expect(bodies[1].messages[1].content).toContain("I think I am done. I have not run the tests since my last change.");
    await fire("turn_start", { turnIndex: 3 });
    expect(await fire("turn_end", { message: done, toolResults: [] })).toBeUndefined(); // once

    const events = readEvents(dir);
    for (const e of events) expect(validate(e), JSON.stringify(validate.errors)).toBe(true);
    expect(events.filter((e) => e.type === "trigger_fired").map((e) => [e.intervention, e.reason, e.turn])).toEqual([
      ["orient", "before the first edit", 1],
      ["before_done", "stopped after editing", 2],
    ]);
    expect(events.filter((e) => e.type === "advice_applied").map((e) => e.code_lines_removed)).toEqual([0, 2]);
  });

  it("the consult tool refuses under the consult rules, and the executor sees why", async () => {
    const dir = mkdtempSync(join(tmpdir(), "pi-ext-"));
    const { url, bodies } = await advisor(["Look at the operator."]);
    const env = writeConfig(dir, url, ["consult"], true, {
      rules: { min_own_actions: 1, tool_cooldown_turns: 2, require_hypothesis: true, max_advice_code_lines: 5 },
    });
    const { pi, tools, fire } = fakePi();
    advisorExtension(pi, env);
    await fire("turn_start", { turnIndex: 0 });
    const args = { question: "Why -1?", tried: "I read math.sh and ran add 2 3.", hypothesis: "The function subtracts instead of adding." };
    const first = await tools[0].execute("c1", args, undefined);
    expect(first.content[0].text).toMatch(/^Investigate first/);
    await fire("tool_result", { toolName: "bash", input: { command: "cat math.sh" }, content: [{ type: "text", text: "x" }], isError: false });
    const vague = await tools[0].execute("c2", { question: "Why?", tried: "stuff", hypothesis: "none yet" }, undefined);
    expect(vague.content[0].text).toMatch(/^Say what you tried/);
    const ok = await tools[0].execute("c3", args, undefined);
    expect(ok.content[0].text).toBe("ADVICE: Look at the operator. (4 left)");
    await fire("turn_start", { turnIndex: 1 });
    await fire("tool_result", { toolName: "bash", input: { command: "cat math.sh" }, content: [{ type: "text", text: "x" }], isError: false });
    const soon = await tools[0].execute("c4", args, undefined);
    expect(soon.content[0].text).toMatch(/^Too soon after the last consult/);
    expect(bodies).toHaveLength(1);
    const events = readEvents(dir);
    for (const e of events) expect(validate(e), JSON.stringify(validate.errors)).toBe(true);
    expect(events.filter((e) => e.type === "consult_refused").map((e) => e.reason)).toEqual([
      "min_own_actions",
      "require_hypothesis",
      "tool_cooldown_turns",
    ]);
  });

  it("an unreachable advisor gives the executor a plain answer and an advisor_error", async () => {
    const dir = mkdtempSync(join(tmpdir(), "pi-ext-"));
    const env = writeConfig(dir, "http://127.0.0.1:9/v1", ["consult"]);
    const { pi, tools } = fakePi();
    advisorExtension(pi, env);
    const result = await tools[0].execute("c1", { question: "q" }, undefined);
    expect(result.content[0].text).toMatch(/could not be reached/);
    const events = readFileSync(join(dir, "events.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l));
    for (const e of events) expect(validate(e), JSON.stringify(validate.errors)).toBe(true);
    expect(events.map((e) => e.type)).toEqual(["policy_rendered", "consult_requested", "brief_built", "advisor_request", "advisor_error"]);
    expect(events[4]).toMatchObject({ status: null });
  });

  it("no consult tool without the consult intervention; nothing at all without an advisor", () => {
    const dir = mkdtempSync(join(tmpdir(), "pi-ext-"));
    const a = fakePi();
    advisorExtension(a.pi, writeConfig(dir, "http://x/v1", ["stuck"]));
    expect(a.tools).toEqual([]);
    expect([...a.handlers.keys()].sort()).toEqual(["before_agent_start", "tool_call", "tool_result", "turn_end", "turn_start"]);
    const b = fakePi();
    advisorExtension(b.pi, writeConfig(dir, "http://x/v1", [], false));
    expect(b.handlers.size + b.tools.length).toBe(0);
  });

  it("a missing config fails loudly at load", () => {
    expect(() => advisorExtension(fakePi().pi, { LSO_ADVISOR_CONFIG: "/nonexistent.json" })).toThrow(/nonexistent/);
  });

  it("task text is the issue inside the adapter's prompt", () => {
    expect(taskText(PROMPT)).toBe("`parse_value()` crashes on empty input.");
    expect(taskText("  plain prompt ")).toBe("plain prompt");
  });
});

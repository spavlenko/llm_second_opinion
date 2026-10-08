// The advisor as a pi extension: reads advisor.json, registers the consult tool, feeds pi's
// lifecycle events to advisor-core, and injects the advice it returns.
//
// - Executor guidance (`executor_guidance` slot) is appended to pi's system prompt in
//   `before_agent_start` (systemPromptOptions.appendSystemPrompt).
// - The plan review runs in that same handler, before the first model call; its advice is the
//   handler's custom message, which pi adds to the context next to the prompt.
// - Consult tool: the advice is the tool result.
// - Harness triggers (orient, before_done, stuck, on_test_failure, periodic) are checked in
//   `turn_end`; their advice is a custom message entry appended at that boundary, with
//   `continue: true` so the model sees it in its next request even if the turn would have ended
//   the run. A turn whose assistant message has no tool calls is the executor stopping, which is
//   when before_done fires (and its continuation gives the executor one more turn).
// - report_gate: while the executor has filed no report with the consult tool, `tool_call`
//   blocks its edits, and the block reason asks for the report.
// - orient, if it has not fired by the executor's first edit, fires in `tool_call` for that
//   edit: the edit is blocked, and the block reason (the tool result) carries the advice and asks
//   the executor to re-issue the edit if it still fits.
//
// Environment: LSO_ADVISOR_CONFIG (default /run/advisor.json); LSO_ADVISOR_BASE_URL overrides the
// advisor endpoint's base_url (the host as the container reaches it); LSO_ADVICE_LOG, if set, is a
// JSONL file with every consult in full (messages sent, advice received and injected).
import { appendFileSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext, ToolResultEvent } from "@earendil-works/pi-coding-agent";
import {
  type AdviceRecord,
  AdvisorClient,
  AdvisorSession,
  CONSULT_TOOL,
  EventWriter,
  isAdvisorArm,
  loadRunConfig,
} from "@llm-second-opinion/advisor-core";
import { Type } from "typebox";

export const DEFAULT_CONFIG = "/run/advisor.json";
/** customType of the messages that carry advice into pi's context. */
export const ADVICE_MESSAGE = "lso-advice";
/** Leads the tool result of the first edit when orient holds it for advice. */
export const EDIT_HELD =
  "This edit was not applied: the advisor reviewed your findings before your first change. " +
  "Read the advice, then re-issue the edit if it still fits.";
const MAX_FILE_BYTES = 2_000_000;

type Env = Record<string, string | undefined>;

// Same pattern as agents/pi/toolcall-nudge.ts.
const TEXT_TOOL_CALL = /<tool_call>|<function=[\w.-]+>/;

export default function advisorExtension(pi: ExtensionAPI, env: Env = process.env): void {
  const config = loadRunConfig(env.LSO_ADVISOR_CONFIG || DEFAULT_CONFIG);
  if (!isAdvisorArm(config)) return; // an arm without an advisor: nothing to do

  const events = new EventWriter(config.events_path);
  const adviceLog = env.LSO_ADVICE_LOG;
  const cwd = process.cwd();
  const session = new AdvisorSession({
    config,
    emit: (e) => events.emit(e),
    client: new AdvisorClient(config.advisor_model, { baseUrl: env.LSO_ADVISOR_BASE_URL, env }),
    readFile: (path) => readSmallFile(resolve(cwd, path)),
    log: adviceLog ? (r: AdviceRecord) => appendFileSync(adviceLog, `${JSON.stringify(r)}\n`) : undefined,
  });

  let started = false;
  let turns = 0;

  pi.on("before_agent_start", async (event, ctx) => {
    const guidance = session.executorGuidance();
    const options = event.systemPromptOptions;
    if (guidance && !options.appendSystemPrompt.includes(guidance)) {
      options.appendSystemPrompt = [options.appendSystemPrompt, guidance].filter(Boolean).join("\n\n");
    }
    if (started) return;
    started = true;
    session.renderPolicy(); // the guidance above and the tool description below, as the executor sees them
    session.setTask(taskText(event.prompt));
    const advice = await guarded("plan", () => session.atStart(ctx.signal));
    if (!advice) return;
    session.applied(advice);
    return { message: { customType: ADVICE_MESSAGE, content: advice.text, display: true } };
  });

  pi.on("turn_start", async () => {
    session.turnStart(turns++);
  });

  pi.on("tool_result", async (event) => {
    if (event.toolName === CONSULT_TOOL || event.parentToolCallId) return;
    session.observe({ name: event.toolName, args: event.input, result: resultText(event), isError: event.isError });
  });

  pi.on("tool_call", async (event, ctx: ExtensionContext) => {
    if (event.toolName === CONSULT_TOOL || event.parentToolCallId) return;
    if (!session.isEdit(event.toolName, event.input as Record<string, unknown>)) return;
    const gate = session.reportGate();
    if (gate) return { block: true, reason: gate };
    const advice = await guarded("tool_call", () => session.beforeEdit(ctx.signal));
    if (!advice) return;
    session.applied(advice);
    return { block: true, reason: `${EDIT_HELD}\n\n${advice.text}` };
  });

  pi.on("turn_end", async (event, ctx: ExtensionContext) => {
    const message = event.message as { stopReason?: string; content?: unknown };
    const stop = message.stopReason;
    if (ctx.signal?.aborted || stop === "aborted" || stop === "error") return;
    const content = Array.isArray(message.content) ? (message.content as { type?: string; text?: string; thinking?: string }[]) : [];
    session.note(
      content.map((c) => (c.type === "text" ? (c.text ?? "") : "")).join("\n"),
      content.map((c) => (c.type === "thinking" ? (c.thinking ?? "") : "")).join("\n"),
    );
    // A tool call written as text is not the executor stopping: the harness's nudge extension
    // sends it back, so before_done must not fire on it.
    const text = content.map((c) => (c.type === "text" ? (c.text ?? "") : "")).join("\n");
    const stopping =
      stop !== "toolUse" &&
      !content.some((c) => c.type === "toolCall") &&
      !event.toolResults?.length &&
      !TEXT_TOOL_CALL.test(text);
    const advice = await guarded("turn_end", () => session.atTurnEnd(ctx.signal, stopping));
    if (!advice) return;
    session.applied(advice);
    return {
      entries: [{ type: "custom_message", customType: ADVICE_MESSAGE, content: advice.text, display: true }],
      continue: true,
    };
  });

  if (session.consultEnabled) {
    pi.registerTool({
      name: CONSULT_TOOL,
      label: "Consult advisor",
      description: session.consultToolDescription(),
      parameters: Type.Object({
        // The slot text describes the arguments; these are short labels only.
        question: Type.String({ description: "One specific question." }),
        tried: Type.Optional(Type.String({ description: "What you have done so far and what happened." })),
        hypothesis: Type.Optional(Type.String({ description: "What you think causes the bug." })),
      }),
      executionMode: "sequential",
      async execute(_toolCallId, params, signal) {
        const result = await guarded("consult tool", () => session.consultTool(params, signal));
        if (result?.advice) session.applied(result.advice);
        const text = result?.text ?? "The advisor could not be reached. Continue on your own.";
        return { content: [{ type: "text", text }], details: { request_id: result?.advice?.requestId ?? null } };
      },
    });
  }
}

/** The issue inside the adapter's prompt (`<issue>...</issue>`), or the whole prompt. */
export function taskText(prompt: string): string {
  const m = /<issue>\s*([\s\S]*?)\s*<\/issue>/.exec(prompt);
  return m ? m[1]! : prompt.trim();
}

function resultText(event: Pick<ToolResultEvent, "content">): string {
  return event.content.map((c) => (c.type === "text" ? c.text : "")).join("\n");
}

function readSmallFile(path: string): string | null {
  try {
    if (statSync(path).size > MAX_FILE_BYTES) return null;
    return readFileSync(path, "utf8");
  } catch {
    return null;
  }
}

/** The advisor must never take the agent down: a bug here is reported and the run goes on. */
async function guarded<T>(where: string, fn: () => Promise<T>): Promise<T | null> {
  try {
    return await fn();
  } catch (e) {
    process.stderr.write(`lso-advisor: ${where} failed: ${(e as Error).stack ?? e}\n`);
    return null;
  }
}

// One run's advisor: observations in, consults out. The binding feeds it its agent's events
// and injects the text it returns; everything sent to the advisor goes through here and is
// recorded as events (the exposure record) before it is sent.
import { limitCodeBlocks } from "./advice.js";
import {
  type Brief,
  type BriefContext,
  type BriefRequest,
  approxTokens,
  buildBrief,
  errorCategory,
} from "./brief.js";
import { type AdvisorClientLike, AdvisorClientError, type ChatMessage, type Completion } from "./client.js";
import type { AdvisorRunConfig } from "./config.js";
import type { Intervention, PromptSet } from "./contracts.js";
import type { EventInput } from "./events.js";
import { editsFiles, errorText, testRun, type ToolObservation } from "./observe.js";
import { Redactor, RoleMap } from "./redact.js";
import { renderTemplate } from "./template.js";
import { type Decision, TriggerEngine } from "./triggers.js";

export const CONSULT_TOOL = "consult";
/** Appended to advice the provider cut at max_answer_tokens (finish_reason "length"), so the
 * executor knows it is incomplete. */
export const TRUNCATED_MARKER = "[advice truncated: the advisor hit its answer length limit]";
const RECENT = 12;
/** The executor's notes kept for the orient brief: its last few texts, each capped. */
const NOTES = 3;
const NOTE_CHARS = 600;
/** Edits kept for `{{edits}}`. */
const EDITS = 4;
/** `clarify`: the most lines of a file, or of test output, sent in a follow-up. */
export const CLARIFY_MAX_LINES = 40;

/** The consult tool's arguments, as the executor wrote them. */
export interface ConsultArgs {
  question: string;
  tried?: string;
  hypothesis?: string;
}

/** Advice ready to inject, and the request it answers (for `applied`). */
export interface Advice {
  requestId: string;
  text: string;
  /** Code lines cut by max_advice_code_lines. */
  codeLinesRemoved: number;
}

/** One consult in full, for the plugin's own log next to events.jsonl: the exact messages
 * sent and the advice as received and as injected. */
export interface AdviceRecord {
  request_id: string;
  intervention: Intervention;
  turn: number;
  level: string;
  prompt_hash: string;
  system: string;
  brief: string;
  /** Earlier exchanges re-sent with the brief (`memory`). */
  history_turns: number;
  /** `clarify`: what the advisor asked for and what was sent back. */
  followup: { requested: string; sent: string } | null;
  advice: string | null;
  injected: string | null;
  error: string | null;
  /** Placeholder to real identifier, the whole session's map as of this consult. */
  role_map: Record<string, string>;
}

/** What the advisor may ask for with `clarify`: a file range, or the latest test output. */
export type ClarifyRequest = { kind: "file"; path: string; start: number; end: number } | { kind: "test_output" };

/** The advisor's reply read as a `clarify` request: exactly one line, `FILE <path>:<start>-<end>`
 * or `TEST_OUTPUT` (backticks around it allowed). Anything else is an answer. */
export function parseClarify(reply: string): ClarifyRequest | null {
  const line = reply.trim().replace(/^`+|`+$/g, "").trim();
  if (line.includes("\n")) return null;
  if (/^TEST_OUTPUT$/.test(line)) return { kind: "test_output" };
  const m = /^FILE\s+(\S+):(\d+)\s*-\s*(\d+)$/.exec(line);
  return m ? { kind: "file", path: m[1]!, start: Number(m[2]), end: Number(m[3]) } : null;
}

export interface AdvisorSessionOptions {
  config: AdvisorRunConfig;
  emit: (event: EventInput) => void;
  client: AdvisorClientLike;
  readFile?: (path: string) => string | null;
  log?: (record: AdviceRecord) => void;
  /** Milliseconds, for advisor_error latency when the client reports none. */
  now?: () => number;
}

export class AdvisorSession {
  readonly engine: TriggerEngine;
  readonly roles: RoleMap;
  readonly prompts: PromptSet;
  private task = "";
  private recent: ToolObservation[] = [];
  private lastFailure: ToolObservation | null = null;
  private lastTestRun: ToolObservation | null = null;
  private failedTests: string[] = [];
  private lastEdit: BriefContext["lastEdit"] = null;
  private notes: string[] = [];
  private thinking = "";
  private edits: { path: string; text: string }[] = [];
  /** Earlier briefs and the advisor's answers to them, for `memory`. */
  private history: { brief: string; answer: string }[] = [];
  private requests = 0;
  private policyDone = false;

  constructor(private readonly opts: AdvisorSessionOptions) {
    const { advisor, run } = opts.config;
    this.engine = new TriggerEngine(advisor);
    this.prompts = opts.config.prompts;
    this.roles = new RoleMap({
      surrogates: advisor.surrogates && advisor.level !== "L3",
      seed: `${run.experiment}/${run.arm}/${run.task}/${run.seed}`,
    });
    if (advisor.level !== "L3") this.roles.seedProject(run.task);
  }

  private get settings() {
    return this.opts.config.advisor;
  }

  get consultEnabled(): boolean {
    return this.settings.interventions.includes("consult");
  }

  // Each slot gets exactly its placeholders from docs/spec.md (Research design); the harness
  // rejects any other name when it loads a prompt set. `brief` is rendered by buildBrief.

  executorGuidance(): string {
    const { max_consults, field_target_words } = this.settings;
    return renderTemplate(this.prompts.texts.executor_guidance, { max_consults, field_target_words }).trim();
  }

  consultToolDescription(): string {
    const { max_consults, field_target_words } = this.settings;
    return renderTemplate(this.prompts.texts.consult_tool, { max_consults, field_target_words }).trim();
  }

  advisorSystem(): string {
    const { level, max_answer_tokens, answer_target_words, clarify } = this.settings;
    return renderTemplate(this.prompts.texts.advisor_system, {
      level,
      max_answer_tokens: max_answer_tokens ?? "unlimited",
      // null: no target, and a "...: {{answer_target_words}}" line is dropped.
      answer_target_words,
      // The {{#clarify}} section: kept only when the arm allows a follow-up.
      clarify: clarify ? "yes" : "",
    }).trim();
  }

  /** policy_rendered, once, before anything else the session emits: the help-policy text the
   * executor sees. The guidance is what the binding appends to the system prompt; the tool
   * description is null when the consult tool is not registered. The binding calls it at session
   * start, and every entry point below calls it too, so it always comes first. */
  renderPolicy(): void {
    if (this.policyDone) return;
    this.policyDone = true;
    this.opts.emit({
      type: "policy_rendered",
      prompt_hash: this.prompts.hash,
      executor_guidance: this.executorGuidance(),
      consult_tool: this.consultEnabled ? this.consultToolDescription() : null,
    });
  }

  /** The task text the briefs summarize (the issue, from the executor's first prompt). */
  setTask(text: string): void {
    this.task = text;
    this.roles.noteText(text);
  }

  turnStart(turn: number): void {
    this.engine.turnStart(turn);
  }

  /** The executor's own text in a turn (not its tool calls): its findings, for `orient`. */
  note(text: string, thinking = ""): void {
    if (thinking.trim()) {
      this.thinking = thinking.trim();
      this.roles.noteText(this.thinking);
    }
    const t = text.trim();
    this.roles.noteText(t);
    if (t) this.notes = [...this.notes, t.length > NOTE_CHARS ? `${t.slice(0, NOTE_CHARS)}…` : t].slice(-NOTES);
  }

  observe(obs: ToolObservation): void {
    this.engine.observe(obs);
    this.roles.noteText(`${JSON.stringify(obs.args)}\n${obs.result}`);
    this.recent = [...this.recent, obs].slice(-RECENT);
    const run = testRun(obs);
    if (obs.isError || run?.failed) this.lastFailure = obs;
    if (run) {
      this.failedTests = run.failedTests;
      this.lastTestRun = obs;
    }
    const path = typeof obs.args.path === "string" ? obs.args.path : null;
    if (path && !obs.isError && (obs.name === "edit" || obs.name === "write")) {
      const edits = obs.args.edits as { newText?: string }[] | undefined;
      const text = obs.name === "write" ? String(obs.args.content ?? "") : (edits ?? []).map((e) => e.newText ?? "").join("\n");
      this.lastEdit = { path, text };
      this.edits = [...this.edits, { path, text }].slice(-EDITS);
    }
  }

  private context(intervention: Intervention): BriefContext {
    // Before stopping, a test run that passed after the last edit makes older failures stale.
    const stale = intervention === "before_done" && this.engine.testSinceEdit?.failed === false;
    return {
      task: this.task,
      recent: this.recent,
      lastFailure: stale ? null : this.lastFailure,
      failedTests: stale ? [] : this.failedTests,
      lastEdit: this.lastEdit,
      notes: this.notes,
      thinking: this.thinking,
      edits: this.edits,
    };
  }

  /** trigger_skipped for every harness trigger the engine passed over since the last call. */
  private emitSkips(): void {
    for (const s of this.engine.takeSkips()) {
      this.opts.emit({ type: "trigger_skipped", intervention: s.intervention, reason: s.reason, turn: s.turn });
    }
  }

  /** Before the first model call: the plan review, if configured. */
  async atStart(signal?: AbortSignal): Promise<Advice | null> {
    this.renderPolicy();
    const decision = this.engine.start();
    this.emitSkips();
    return this.onDecision(decision, {}, signal);
  }

  /** At the end of a turn: a harness trigger, if one is due. `stopping`: the turn made no tool
   * calls (the executor is about to finish), which is when before_done fires. */
  async atTurnEnd(signal?: AbortSignal, stopping = false): Promise<Advice | null> {
    this.renderPolicy();
    const decision = this.engine.turnEnd(stopping);
    this.emitSkips();
    const words = decision?.kind === "fire" && decision.fire.intervention === "before_done" ? { question: this.beforeDoneQuestion() } : {};
    return this.onDecision(decision, words, signal);
  }

  /** The executor is about to make its first edit: orient, if it has not fired yet. */
  async beforeEdit(signal?: AbortSignal): Promise<Advice | null> {
    this.renderPolicy();
    const decision = this.engine.beforeEdit();
    this.emitSkips();
    return this.onDecision(decision, {}, signal);
  }

  /** Is this call one that changes files (so `beforeEdit` applies)? */
  isEdit(name: string, args: Record<string, unknown>): boolean {
    return editsFiles({ name, args, result: "", isError: false });
  }

  /** before_done's question. It is skipped when the tests passed after the last edit, so the
   * tests either failed then or were not run. */
  private beforeDoneQuestion(): string {
    const tests = this.engine.testSinceEdit?.failed
      ? "The last test run after my change failed (output above)."
      : "I have not run the tests since my last change.";
    return `I think I am done. ${tests} Sanity-check my approach given that: is the fix in the right place, and what might I have missed?`;
  }

  /** The consult tool. Returns the text for the tool result: the advice, or why there is none.
   * A request the consult rules or the budget turn down is `consult_refused` (with
   * `budget_exhausted` the first time the budget does) and uses up nothing. */
  async consultTool(args: ConsultArgs, signal?: AbortSignal): Promise<{ text: string; advice: Advice | null }> {
    this.renderPolicy();
    this.roles.noteText([args.question, args.tried, args.hypothesis].filter(Boolean).join("\n"));
    const decision = this.engine.requestConsult(args);
    if (decision.kind === "refused") {
      this.opts.emit({ type: "consult_refused", reason: decision.rule, turn: this.engine.turn });
      return { text: decision.message, advice: null };
    }
    if (decision.kind === "exhausted") {
      this.opts.emit({ type: "consult_refused", reason: "max_consults", turn: this.engine.turn });
      await this.onDecision(decision, {}, signal);
      return { text: `No advisor consults left (${decision.limit} used). Continue on your own.`, advice: null };
    }
    this.opts.emit({ type: "consult_requested", reason: args.question ?? "", turn: this.engine.turn });
    const advice = await this.onDecision(
      decision,
      { question: args.question, tried: args.tried, hypothesis: args.hypothesis },
      signal,
    );
    if (advice) return { text: advice.text, advice };
    return { text: "The advisor could not be reached. Continue on your own.", advice: null };
  }

  /** The binding injected the advice. */
  applied(advice: Advice): void {
    this.opts.emit({
      type: "advice_applied",
      request_id: advice.requestId,
      turn: this.engine.turn,
      injected_text: advice.text,
      code_lines_removed: advice.codeLinesRemoved,
    });
  }

  private async onDecision(
    decision: Decision | null,
    words: Pick<BriefRequest, "question" | "tried" | "hypothesis">,
    signal?: AbortSignal,
  ): Promise<Advice | null> {
    if (!decision) return null;
    if (decision.kind === "exhausted") {
      if (decision.report) this.opts.emit({ type: "budget_exhausted", consults_used: decision.used, limit: decision.limit });
      return null;
    }
    const { fire } = decision;
    if (fire.intervention !== "consult") {
      this.opts.emit({ type: "trigger_fired", intervention: fire.intervention, reason: fire.reason, turn: fire.turn });
    }
    const brief = this.brief({ ...fire, ...words });
    return this.consult(fire.intervention, brief, signal);
  }

  brief(req: BriefRequest): Brief {
    const brief = buildBrief(
      this.settings.level,
      this.prompts.texts.brief,
      req,
      this.context(req.intervention),
      this.roles,
      this.opts.readFile,
      this.settings.max_brief_tokens,
    );
    this.opts.emit({
      type: "brief_built",
      level: this.settings.level,
      tokens: brief.tokens,
      identifiers_redacted: brief.identifiersRedacted,
      role_map_size: this.roles.size,
      truncated: brief.truncated,
      role_map: this.roles.mapFor(brief.text),
    });
    return brief;
  }

  /** `clarify`: the item the advisor asked for, fetched and redacted at the arm's level. */
  followupText(ask: ClarifyRequest): string {
    const level = this.settings.level;
    const verbatim = level === "L3";
    const r = new Redactor(this.roles);
    let body: string;
    if (ask.kind === "file") {
      body = this.fileExcerpt(ask, r);
    } else {
      const run = this.lastTestRun;
      if (!run) body = "The tests have not been run yet.";
      else if (level === "L0") body = testRun(run)?.failed ? errorCategory(run, this.failedTests.length) : "The tests pass.";
      else if (level === "L1") body = r.prose(errorText(run.result, 8));
      else {
        const tail = run.result.split("\n").map((l) => l.trimEnd()).filter(Boolean).slice(-CLARIFY_MAX_LINES).join("\n");
        body = verbatim ? tail : r.prose(tail);
      }
    }
    const what = ask.kind === "file" ? "the file excerpt" : "the latest test output";
    const text = `Here is ${what} you asked for:\n\n${body}\n\nNow answer the question in the brief.`;
    return verbatim ? text : this.roles.sweep(text).text;
  }

  private fileExcerpt(ask: Extract<ClarifyRequest, { kind: "file" }>, r: Redactor): string {
    const level = this.settings.level;
    if (level === "L0" || level === "L1") return `Code is not shared at this level (${level}).`;
    const path = level === "L3" ? ask.path : this.roles.restore(ask.path);
    const unavailable = "That file is not available.";
    if (path.startsWith("/") || path.split("/").includes("..")) return unavailable;
    const content = this.opts.readFile?.(path) ?? null;
    if (content === null) return unavailable;
    const lines = content.split("\n");
    const from = Math.max(1, Math.min(ask.start, ask.end));
    const to = Math.min(lines.length, Math.max(ask.start, ask.end), from + CLARIFY_MAX_LINES - 1);
    if (from > to) return `${unavailable} It has ${lines.length} lines.`;
    const width = String(to).length;
    const verbatim = level === "L3";
    const numbered = lines
      .slice(from - 1, to)
      .map((l, i) => `${String(from + i).padStart(width)} | ${verbatim ? l : r.code(l)}`)
      .join("\n");
    return `${verbatim ? path : r.file(path)}:${from}-${to}\n${numbered}`;
  }

  private async consult(intervention: Intervention, brief: Brief, signal?: AbortSignal): Promise<Advice | null> {
    const requestId = `r${++this.requests}`;
    const system = this.advisorSystem();
    const history: ChatMessage[] = this.settings.memory
      ? this.history.flatMap((h): ChatMessage[] => [
          { role: "user", content: h.brief },
          { role: "assistant", content: h.answer },
        ])
      : [];
    const historyTurns = history.length / 2;
    const record: AdviceRecord = {
      request_id: requestId,
      intervention,
      turn: this.engine.turn,
      level: this.settings.level,
      prompt_hash: this.prompts.hash,
      system,
      brief: brief.text,
      history_turns: historyTurns,
      followup: null,
      advice: null,
      injected: null,
      error: null,
      role_map: this.roles.toObject(),
    };
    const now = this.opts.now ?? (() => performance.now());
    const start = now();
    // Recorded before it is sent: what left the machine is in events.jsonl even if the run dies.
    this.opts.emit({
      type: "advisor_request",
      request_id: requestId,
      // An estimate (4 characters a token); advisor_response.prompt_tokens is the provider's count.
      input_tokens: approxTokens(system) + brief.tokens + history.reduce((n, m) => n + approxTokens(m.content), 0),
      brief_text: brief.text,
      prompt_hash: this.prompts.hash,
      history_turns: historyTurns,
    });
    const call = async (continuation: ChatMessage[]): Promise<Completion> => {
      const done = await this.opts.client.complete({
        system,
        user: brief.text,
        history,
        continuation,
        maxTokens: this.settings.max_answer_tokens,
        requestId,
        signal,
      });
      this.opts.emit({
        type: "advisor_response",
        request_id: requestId,
        output_tokens: done.outputTokens,
        cached_tokens: done.cachedTokens,
        latency_ms: done.latencyMs,
        prompt_tokens: done.promptTokens,
        reasoning_tokens: done.reasoningTokens,
        finish_reason: done.finishReason,
        advice_text: done.text,
      });
      return done;
    };
    try {
      let done = await call([]);
      // clarify: one follow-up at most, so a second request is taken as the answer.
      const ask = this.settings.clarify ? parseClarify(done.text) : null;
      if (ask) {
        const requested = done.text.trim();
        const sent = this.followupText(ask);
        this.opts.emit({ type: "advisor_followup", request_id: requestId, requested, sent_text: sent, tokens: approxTokens(sent) });
        record.followup = { requested, sent };
        record.role_map = this.roles.toObject();
        done = await call([
          { role: "assistant", content: done.text },
          { role: "user", content: sent },
        ]);
      }
      this.history.push({ brief: brief.text, answer: done.text });
      const restored = this.roles.restore(done.text.trim());
      const code = limitCodeBlocks(restored, this.settings.rules.max_advice_code_lines);
      const advice = done.finishReason === "length" ? `${code.text}\n${TRUNCATED_MARKER}` : code.text;
      const text = renderTemplate(this.prompts.texts.advice_injection, {
        advice,
        consults_left: this.engine.consultsLeft,
      }).trim();
      this.log({ ...record, advice: done.text, injected: text });
      return { requestId, text, codeLinesRemoved: code.removed };
    } catch (e) {
      const message = (e as Error).message || String(e);
      const known = e instanceof AdvisorClientError;
      this.opts.emit({
        type: "advisor_error",
        request_id: requestId,
        message,
        status: known ? e.status : null,
        latency_ms: known && e.latencyMs !== null ? e.latencyMs : Math.max(0, now() - start),
      });
      this.log({ ...record, error: message });
      return null;
    }
  }

  private log(record: AdviceRecord): void {
    try {
      this.opts.log?.(record);
    } catch {
      // The log is a convenience; events.jsonl is the record.
    }
  }
}

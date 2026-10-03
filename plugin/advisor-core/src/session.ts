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
} from "./brief.js";
import { type AdvisorClientLike, AdvisorClientError } from "./client.js";
import type { AdvisorRunConfig } from "./config.js";
import type { Intervention, PromptSet } from "./contracts.js";
import type { EventInput } from "./events.js";
import { editsFiles, testRun, type ToolObservation } from "./observe.js";
import { RoleMap } from "./redact.js";
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
  advice: string | null;
  injected: string | null;
  error: string | null;
  /** Placeholder to real identifier, the whole session's map as of this consult. */
  role_map: Record<string, string>;
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
  readonly roles = new RoleMap();
  readonly prompts: PromptSet;
  private task = "";
  private recent: ToolObservation[] = [];
  private lastFailure: ToolObservation | null = null;
  private failedTests: string[] = [];
  private lastEdit: BriefContext["lastEdit"] = null;
  /** The latest run of the task's tests since the latest edit: null if none. */
  private testSinceEdit: { failed: boolean } | null = null;
  private notes: string[] = [];
  private requests = 0;
  private policyDone = false;

  constructor(private readonly opts: AdvisorSessionOptions) {
    this.engine = new TriggerEngine(opts.config.advisor);
    this.prompts = opts.config.prompts;
    if (opts.config.advisor.level !== "L3") this.roles.seedProject(opts.config.run.task);
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
    const { level, max_answer_tokens, answer_target_words } = this.settings;
    return renderTemplate(this.prompts.texts.advisor_system, {
      level,
      max_answer_tokens: max_answer_tokens ?? "unlimited",
      // null: no target, and a "...: {{answer_target_words}}" line is dropped.
      answer_target_words,
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
  }

  turnStart(turn: number): void {
    this.engine.turnStart(turn);
  }

  /** The executor's own text in a turn (not its tool calls): its findings, for `orient`. */
  note(text: string): void {
    const t = text.trim();
    if (t) this.notes = [...this.notes, t.length > NOTE_CHARS ? `${t.slice(0, NOTE_CHARS)}…` : t].slice(-NOTES);
  }

  observe(obs: ToolObservation): void {
    this.engine.observe(obs);
    this.recent = [...this.recent, obs].slice(-RECENT);
    const run = testRun(obs);
    if (obs.isError || run?.failed) this.lastFailure = obs;
    if (run) {
      this.failedTests = run.failedTests;
      this.testSinceEdit = { failed: run.failed };
    }
    if (editsFiles(obs)) this.testSinceEdit = null;
    const path = typeof obs.args.path === "string" ? obs.args.path : null;
    if (path && !obs.isError && (obs.name === "edit" || obs.name === "write")) {
      const edits = obs.args.edits as { newText?: string }[] | undefined;
      const text = obs.name === "write" ? String(obs.args.content ?? "") : (edits ?? []).map((e) => e.newText ?? "").join("\n");
      this.lastEdit = { path, text };
    }
  }

  private context(intervention: Intervention): BriefContext {
    // Before stopping, a test run that passed after the last edit makes older failures stale.
    const stale = intervention === "before_done" && this.testSinceEdit?.failed === false;
    return {
      task: this.task,
      recent: this.recent,
      lastFailure: stale ? null : this.lastFailure,
      failedTests: stale ? [] : this.failedTests,
      lastEdit: this.lastEdit,
      notes: this.notes,
    };
  }

  /** Before the first model call: the plan review, if configured. */
  async atStart(signal?: AbortSignal): Promise<Advice | null> {
    this.renderPolicy();
    return this.onDecision(this.engine.start(), {}, signal);
  }

  /** At the end of a turn: a harness trigger, if one is due. `stopping`: the turn made no tool
   * calls (the executor is about to finish), which is when before_done fires. */
  async atTurnEnd(signal?: AbortSignal, stopping = false): Promise<Advice | null> {
    this.renderPolicy();
    const decision = this.engine.turnEnd(stopping);
    const words = decision?.kind === "fire" && decision.fire.intervention === "before_done" ? { question: this.beforeDoneQuestion() } : {};
    return this.onDecision(decision, words, signal);
  }

  /** The executor is about to make its first edit: orient, if it has not fired yet. */
  async beforeEdit(signal?: AbortSignal): Promise<Advice | null> {
    this.renderPolicy();
    return this.onDecision(this.engine.beforeEdit(), {}, signal);
  }

  /** Is this call one that changes files (so `beforeEdit` applies)? */
  isEdit(name: string, args: Record<string, unknown>): boolean {
    return editsFiles({ name, args, result: "", isError: false });
  }

  private beforeDoneQuestion(): string {
    const run = this.testSinceEdit;
    const tests = !run
      ? "I have not run the tests since my last change."
      : run.failed
        ? "The last test run after my change failed (output above)."
        : "The last test run after my change passed.";
    return `I think I am done. ${tests} Sanity-check my approach given that: is the fix in the right place, and what might I have missed?`;
  }

  /** The consult tool. Returns the text for the tool result: the advice, or why there is none.
   * A request the consult rules or the budget turn down is `consult_refused` (with
   * `budget_exhausted` the first time the budget does) and uses up nothing. */
  async consultTool(args: ConsultArgs, signal?: AbortSignal): Promise<{ text: string; advice: Advice | null }> {
    this.renderPolicy();
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

  private async consult(intervention: Intervention, brief: Brief, signal?: AbortSignal): Promise<Advice | null> {
    const requestId = `r${++this.requests}`;
    const system = this.advisorSystem();
    const record: AdviceRecord = {
      request_id: requestId,
      intervention,
      turn: this.engine.turn,
      level: this.settings.level,
      prompt_hash: this.prompts.hash,
      system,
      brief: brief.text,
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
      input_tokens: approxTokens(system) + brief.tokens,
      brief_text: brief.text,
      prompt_hash: this.prompts.hash,
    });
    try {
      const done = await this.opts.client.complete({
        system,
        user: brief.text,
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

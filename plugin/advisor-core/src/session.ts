// One run's advisor: observations in, consults out. The binding feeds it its agent's events
// and injects the text it returns; everything sent to the advisor goes through here and is
// recorded as events (the exposure record) before it is sent.
import {
  type Brief,
  type BriefContext,
  type BriefRequest,
  approxTokens,
  buildBrief,
} from "./brief.js";
import type { AdvisorClientLike } from "./client.js";
import type { AdvisorRunConfig } from "./config.js";
import type { Intervention, PromptSet } from "./contracts.js";
import type { EventInput } from "./events.js";
import { testRun, type ToolObservation } from "./observe.js";
import { RoleMap } from "./redact.js";
import { renderTemplate } from "./template.js";
import { type Decision, TriggerEngine } from "./triggers.js";

export const CONSULT_TOOL = "consult";
const RECENT = 12;

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
}

export interface AdvisorSessionOptions {
  config: AdvisorRunConfig;
  emit: (event: EventInput) => void;
  client: AdvisorClientLike;
  readFile?: (path: string) => string | null;
  log?: (record: AdviceRecord) => void;
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
  private requests = 0;

  constructor(private readonly opts: AdvisorSessionOptions) {
    this.engine = new TriggerEngine(opts.config.advisor);
    this.prompts = opts.config.prompts;
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
    return renderTemplate(this.prompts.texts.executor_guidance, { max_consults: this.settings.max_consults }).trim();
  }

  consultToolDescription(): string {
    return renderTemplate(this.prompts.texts.consult_tool, { max_consults: this.settings.max_consults }).trim();
  }

  advisorSystem(): string {
    const { level, max_answer_tokens } = this.settings;
    return renderTemplate(this.prompts.texts.advisor_system, {
      level,
      max_answer_tokens: max_answer_tokens ?? "unlimited",
    }).trim();
  }

  /** The task text the briefs summarize (the issue, from the executor's first prompt). */
  setTask(text: string): void {
    this.task = text;
  }

  turnStart(turn: number): void {
    this.engine.turnStart(turn);
  }

  observe(obs: ToolObservation): void {
    this.engine.observe(obs);
    this.recent = [...this.recent, obs].slice(-RECENT);
    const run = testRun(obs);
    if (obs.isError || run?.failed) this.lastFailure = obs;
    if (run) this.failedTests = run.failedTests;
    const path = typeof obs.args.path === "string" ? obs.args.path : null;
    if (path && !obs.isError && (obs.name === "edit" || obs.name === "write")) {
      const edits = obs.args.edits as { newText?: string }[] | undefined;
      const text = obs.name === "write" ? String(obs.args.content ?? "") : (edits ?? []).map((e) => e.newText ?? "").join("\n");
      this.lastEdit = { path, text };
    }
  }

  private context(): BriefContext {
    return {
      task: this.task,
      recent: this.recent,
      lastFailure: this.lastFailure,
      failedTests: this.failedTests,
      lastEdit: this.lastEdit,
    };
  }

  /** Before the first model call: the plan review, if configured. */
  async atStart(signal?: AbortSignal): Promise<Advice | null> {
    return this.onDecision(this.engine.start(), {}, signal);
  }

  /** At the end of a turn: a harness trigger, if one is due. */
  async atTurnEnd(signal?: AbortSignal): Promise<Advice | null> {
    return this.onDecision(this.engine.turnEnd(), {}, signal);
  }

  /** The consult tool. Returns the text for the tool result: the advice, or why there is none. */
  async consultTool(args: ConsultArgs, signal?: AbortSignal): Promise<{ text: string; advice: Advice | null }> {
    this.opts.emit({ type: "consult_requested", reason: args.question ?? "", turn: this.engine.turn });
    const decision = this.engine.requestConsult();
    const advice = await this.onDecision(
      decision,
      { question: args.question, tried: args.tried, hypothesis: args.hypothesis },
      signal,
    );
    if (advice) return { text: advice.text, advice };
    if (decision.kind === "exhausted") {
      return { text: `No advisor consults left (${decision.limit} used). Continue on your own.`, advice: null };
    }
    return { text: "The advisor could not be reached. Continue on your own.", advice: null };
  }

  /** The binding injected the advice. */
  applied(advice: Advice): void {
    this.opts.emit({
      type: "advice_applied",
      request_id: advice.requestId,
      turn: this.engine.turn,
      injected_text: advice.text,
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
    const brief = buildBrief(this.settings.level, this.prompts.texts.brief, req, this.context(), this.roles, this.opts.readFile);
    this.opts.emit({
      type: "brief_built",
      level: this.settings.level,
      tokens: brief.tokens,
      identifiers_redacted: brief.identifiersRedacted,
      role_map_size: this.roles.size,
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
    };
    // Recorded before it is sent: what left the machine is in events.jsonl even if the run dies.
    this.opts.emit({
      type: "advisor_request",
      request_id: requestId,
      input_tokens: approxTokens(system) + brief.tokens,
      brief_text: brief.text,
      prompt_hash: this.prompts.hash,
    });
    try {
      const done = await this.opts.client.complete({ system, user: brief.text, maxTokens: this.settings.max_answer_tokens, signal });
      this.opts.emit({
        type: "advisor_response",
        request_id: requestId,
        output_tokens: done.outputTokens,
        cached_tokens: done.cachedTokens,
        latency_ms: done.latencyMs,
        advice_text: done.text,
      });
      const advice = this.roles.restore(done.text.trim());
      const text = renderTemplate(this.prompts.texts.advice_injection, {
        advice,
        consults_left: this.engine.consultsLeft,
      }).trim();
      this.log({ ...record, advice: done.text, injected: text });
      return { requestId, text };
    } catch (e) {
      const message = (e as Error).message || String(e);
      this.opts.emit({ type: "advisor_error", request_id: requestId, message });
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

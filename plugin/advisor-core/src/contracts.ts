/* Generated from schemas/*.schema.json by plugin/scripts/gen-types.mjs. Do not edit.
 * Regenerate with `pnpm gen:types` after `bench schemas`. */

/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Event".
 */
export type Event =
  | PolicyRendered
  | ConsultRequested
  | ConsultRefused
  | TriggerFired
  | BriefBuilt
  | AdvisorRequest
  | AdvisorResponse
  | AdvisorError
  | AdviceApplied
  | BudgetExhausted;
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Intervention".
 */
export type Intervention = "plan" | "consult" | "stuck" | "on_test_failure" | "periodic" | "orient" | "before_done";
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Level".
 */
export type Level = "L0" | "L1" | "L2" | "L3";
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ExitReason".
 */
export type ExitReason = "finished" | "turn_limit" | "time_limit" | "token_limit" | "crash";

/**
 * Index of the shared contracts; use the named types below.
 */
export interface Contracts {
  Event?: Event;
  AgentResult?: AgentResult;
  RunConfig?: RunConfig;
  UsageRecord?: UsageRecord;
}
/**
 * Emitted once at the start of an advisor run: the help-policy text the executor sees.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "PolicyRendered".
 */
export interface PolicyRendered {
  schema_version: "1";
  /**
   * Monotonic per run, starting at 0.
   */
  seq: number;
  /**
   * Unix time in seconds.
   */
  ts: number;
  type: "policy_rendered";
  prompt_hash: string;
  /**
   * As appended to the executor's system prompt.
   */
  executor_guidance: string;
  /**
   * The consult tool's description; None when the tool is not registered.
   */
  consult_tool: string | null;
}
/**
 * The executor called the consult tool.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ConsultRequested".
 */
export interface ConsultRequested {
  schema_version: "1";
  /**
   * Monotonic per run, starting at 0.
   */
  seq: number;
  /**
   * Unix time in seconds.
   */
  ts: number;
  type: "consult_requested";
  /**
   * The executor's stated reason, as it wrote it.
   */
  reason: string;
  turn: number;
}
/**
 * The consult tool turned a request down (anti-delegation rules or budget).
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ConsultRefused".
 */
export interface ConsultRefused {
  schema_version: "1";
  /**
   * Monotonic per run, starting at 0.
   */
  seq: number;
  /**
   * Unix time in seconds.
   */
  ts: number;
  type: "consult_refused";
  /**
   * Which rule refused it, e.g. `min_own_actions`.
   */
  reason: string;
  turn: number;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "TriggerFired".
 */
export interface TriggerFired {
  schema_version: "1";
  /**
   * Monotonic per run, starting at 0.
   */
  seq: number;
  /**
   * Unix time in seconds.
   */
  ts: number;
  type: "trigger_fired";
  intervention: Intervention;
  reason: string;
  turn: number;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "BriefBuilt".
 */
export interface BriefBuilt {
  schema_version: "1";
  /**
   * Monotonic per run, starting at 0.
   */
  seq: number;
  /**
   * Unix time in seconds.
   */
  ts: number;
  type: "brief_built";
  level: Level;
  tokens: number;
  identifiers_redacted: number;
  role_map_size: number;
  /**
   * The brief was cut at max_brief_tokens.
   */
  truncated: boolean;
  /**
   * Placeholder to real identifier, for every placeholder in this brief. Stays on the machine; the leakage and re-identification scorers need it.
   */
  role_map: {
    [k: string]: string;
  };
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "AdvisorRequest".
 */
export interface AdvisorRequest {
  schema_version: "1";
  /**
   * Monotonic per run, starting at 0.
   */
  seq: number;
  /**
   * Unix time in seconds.
   */
  ts: number;
  type: "advisor_request";
  request_id: string;
  input_tokens: number;
  /**
   * The exact text sent to the advisor.
   */
  brief_text: string;
  /**
   * Hash of the prompt set that produced the request.
   */
  prompt_hash: string;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "AdvisorResponse".
 */
export interface AdvisorResponse {
  schema_version: "1";
  /**
   * Monotonic per run, starting at 0.
   */
  seq: number;
  /**
   * Unix time in seconds.
   */
  ts: number;
  type: "advisor_response";
  request_id: string;
  output_tokens: number;
  cached_tokens: number;
  latency_ms: number;
  /**
   * The provider's own count, when it reports one.
   */
  prompt_tokens: number | null;
  reasoning_tokens: number | null;
  /**
   * The provider's choices[0].finish_reason, e.g. 'stop', or 'length' when the answer was cut at max_answer_tokens.
   */
  finish_reason: string | null;
  /**
   * The advisor's answer exactly as received (placeholders not yet mapped back).
   */
  advice_text: string;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "AdvisorError".
 */
export interface AdvisorError {
  schema_version: "1";
  /**
   * Monotonic per run, starting at 0.
   */
  seq: number;
  /**
   * Unix time in seconds.
   */
  ts: number;
  type: "advisor_error";
  request_id: string;
  message: string;
  /**
   * HTTP status; None if none came back.
   */
  status: number | null;
  latency_ms: number;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "AdviceApplied".
 */
export interface AdviceApplied {
  schema_version: "1";
  /**
   * Monotonic per run, starting at 0.
   */
  seq: number;
  /**
   * Unix time in seconds.
   */
  ts: number;
  type: "advice_applied";
  request_id: string;
  turn: number;
  /**
   * The exact text the executor was given.
   */
  injected_text: string;
  /**
   * Lines of code cut from the advice by max_advice_code_lines.
   */
  code_lines_removed: number;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "BudgetExhausted".
 */
export interface BudgetExhausted {
  schema_version: "1";
  /**
   * Monotonic per run, starting at 0.
   */
  seq: number;
  /**
   * Unix time in seconds.
   */
  ts: number;
  type: "budget_exhausted";
  consults_used: number;
  limit: number;
}
/**
 * Returned by an AgentAdapter after a run.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "AgentResult".
 */
export interface AgentResult {
  schema_version: "1";
  /**
   * Final `git diff` of the workspace.
   */
  diff: string;
  exit_reason: ExitReason;
  agent: AgentInfo;
  turns: number;
  duration_s: number;
  /**
   * Error text when exit_reason is crash.
   */
  detail: string | null;
  /**
   * Per-role totals from the metering proxy; empty when the run was not metered.
   */
  usage: RoleUsage[];
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "AgentInfo".
 */
export interface AgentInfo {
  name: string;
  version: string;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "RoleUsage".
 */
export interface RoleUsage {
  role: "executor" | "advisor";
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
}
/**
 * Written by the harness into the container as advisor.json.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "RunConfig".
 */
export interface RunConfig {
  schema_version: "1";
  run: RunIdentity;
  executor: ModelEndpoint;
  advisor_model: ModelEndpoint | null;
  /**
   * Null when the arm runs without advisor interventions.
   */
  advisor: AdvisorSettings | null;
  /**
   * The resolved prompt set of an advisor arm; null otherwise.
   */
  prompts: PromptSet | null;
  events_path: string;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "RunIdentity".
 */
export interface RunIdentity {
  experiment: string;
  arm: string;
  task: string;
  seed: number;
  config_hash: string;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ModelEndpoint".
 */
export interface ModelEndpoint {
  base_url: string;
  model: string;
  reasoning_effort: string | null;
  /**
   * Name of the environment variable holding the API key. Never the key itself.
   */
  api_key_env: string | null;
  /**
   * Extra request headers with non-secret values.
   */
  headers: {
    [k: string]: string;
  };
  /**
   * Extra request headers with secret values: header name to the name of the environment variable holding the value. Never the value itself.
   */
  header_env: {
    [k: string]: string;
  };
  /**
   * Sampling temperature sent with every call; None: unset.
   */
  temperature: number | null;
  top_p: number | null;
  /**
   * Sampling seed sent with every call, if the server honours one. Unrelated to the experiment's `seeds`, which are replicate indices.
   */
  sampling_seed: number | null;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "AdvisorSettings".
 */
export interface AdvisorSettings {
  level: Level;
  interventions: Intervention[];
  /**
   * Name of a prompt set in the experiment's `prompts`.
   */
  prompts: string;
  max_consults: number;
  /**
   * Safety ceiling on each advisor answer (sent as max_tokens); reaching it is logged. The working limit is the prompt's target, `answer_target_words`.
   */
  max_answer_tokens: number | null;
  /**
   * Answer length the advisor is asked for, in the prompt.
   */
  answer_target_words: number | null;
  /**
   * Safety ceiling on a brief; cutting it is logged.
   */
  max_brief_tokens: number;
  /**
   * Length the consult tool asks for in each field the executor writes.
   */
  field_target_words: number;
  /**
   * Own read actions before the `orient` trigger fires.
   */
  orient_after: number;
  rules: ConsultRules;
  /**
   * Turns after a consult before a harness trigger may fire.
   */
  cooldown_turns: number;
  /**
   * Turns between `periodic` triggers.
   */
  periodic_every: number | null;
  stuck: StuckThresholds;
}
/**
 * Anti-delegation: the executor must do its own work between consults. Strictness is an
 * arm setting, so experiments can compare strict and loose rules.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ConsultRules".
 */
export interface ConsultRules {
  /**
   * Own tool calls since the last consult before the next.
   */
  min_own_actions: number;
  /**
   * Turns after a consult before the consult tool may be used.
   */
  tool_cooldown_turns: number;
  /**
   * The consult tool refuses a request without `tried` and `hypothesis` written by the executor.
   */
  require_hypothesis: boolean;
  /**
   * Code blocks in advice longer than this are cut before injection (0: no code at all; None: no limit). advice_text keeps the original.
   */
  max_advice_code_lines: number | null;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "StuckThresholds".
 */
export interface StuckThresholds {
  repeat_calls: number;
  same_error: number;
  no_diff_turns: number;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "PromptSet".
 */
export interface PromptSet {
  name: string;
  /**
   * 16 hex chars of SHA-256 over the slot texts.
   */
  hash: string;
  texts: PromptTexts;
}
/**
 * The text of every prompt slot, with `{{name}}` placeholders still in place.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "PromptTexts".
 */
export interface PromptTexts {
  executor_guidance: string;
  consult_tool: string;
  brief: string;
  advisor_system: string;
  advice_injection: string;
}
/**
 * One model call through the metering proxy. Counts are the provider's own `usage`.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "UsageRecord".
 */
export interface UsageRecord {
  schema_version: "1";
  /**
   * Call order within the item, from 0.
   */
  seq: number;
  /**
   * Unix time in seconds when the call started.
   */
  ts: number;
  role: "executor" | "advisor";
  /**
   * Model id as sent.
   */
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
  latency_ms: number;
  /**
   * HTTP status from the upstream endpoint; 0 if none came back.
   */
  status: number;
  /**
   * The plugin's request id (header X-LSO-Request-Id) for advisor calls, which joins this record to the advisor_request event.
   */
  request_id: string | null;
  /**
   * The item attempt this call belongs to.
   */
  attempt: number;
}

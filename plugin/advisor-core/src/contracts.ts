/* Generated from schemas/*.schema.json by plugin/scripts/gen-types.mjs. Do not edit.
 * Regenerate with `pnpm gen:types` after `bench schemas`. */

/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Event".
 */
export type Event = TriggerFired | BriefBuilt | AdvisorRequest | AdvisorResponse | AdviceApplied | BudgetExhausted;
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Intervention".
 */
export type Intervention = "plan" | "consult" | "stuck";
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Level".
 */
export type Level = "L0" | "L1" | "L2" | "L3";
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ExitReason".
 */
export type ExitReason = "finished" | "turn_limit" | "time_limit" | "crash";

/**
 * Index of the shared contracts; use the named types below.
 */
export interface Contracts {
  Event?: Event;
  AgentResult?: AgentResult;
  RunConfig?: RunConfig;
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
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "AdvisorSettings".
 */
export interface AdvisorSettings {
  level: Level;
  interventions: Intervention[];
  max_consults: number;
  stuck: StuckThresholds;
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

import { readFileSync } from "node:fs";
import type { AdvisorSettings, Intervention, Level, PromptSet, RunConfig } from "./contracts.js";

export class ConfigError extends Error {}

const LEVELS: readonly Level[] = ["L0", "L1", "L2", "L3"];
const INTERVENTIONS: readonly Intervention[] = ["plan", "consult", "stuck", "on_test_failure", "periodic", "orient", "before_done"];
const SLOTS = ["executor_guidance", "consult_tool", "brief", "advisor_system", "advice_injection"] as const;

/** A run config for an advisor arm: advisor settings and the advisor endpoint are present. */
export type AdvisorRunConfig = RunConfig & {
  advisor: AdvisorSettings;
  advisor_model: NonNullable<RunConfig["advisor_model"]>;
  prompts: PromptSet;
};

/** Reads advisor.json. The harness validated it with Pydantic when writing it; this checks
 * the parts the plugin relies on, so a mismatched harness fails loudly instead of halfway. */
export function loadRunConfig(path: string): RunConfig {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, "utf8"));
  } catch (e) {
    throw new ConfigError(`${path}: ${(e as Error).message}`);
  }
  return validateRunConfig(raw, path);
}

export function validateRunConfig(raw: unknown, where = "advisor.json"): RunConfig {
  const problems: string[] = [];
  const obj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
  const need = (cond: boolean, msg: string) => {
    if (!cond) problems.push(msg);
  };
  const int = (v: unknown, min: number) => Number.isInteger(v) && (v as number) >= min;

  if (!obj(raw)) throw new ConfigError(`${where}: expected a JSON object`);
  need(raw.schema_version === "1", `schema_version must be "1", got ${JSON.stringify(raw.schema_version)}`);
  need(obj(raw.run), "run is missing");
  need(typeof raw.events_path === "string" && raw.events_path !== "", "events_path is missing");
  const endpoint = (v: unknown, name: string) => {
    if (!obj(v)) return problems.push(`${name} is missing`);
    need(typeof v.base_url === "string", `${name}.base_url must be a string`);
    need(typeof v.model === "string", `${name}.model must be a string`);
    need(obj(v.headers ?? {}), `${name}.headers must be an object`);
    need(obj(v.header_env ?? {}), `${name}.header_env must be an object`);
  };
  endpoint(raw.executor, "executor");

  const a = raw.advisor;
  if (a !== null && a !== undefined) {
    if (!obj(a)) {
      problems.push("advisor must be an object or null");
    } else {
      endpoint(raw.advisor_model, "advisor_model");
      need(obj(raw.prompts), "an arm with an advisor needs prompts (the resolved prompt set)");
      need(LEVELS.includes(a.level as Level), `advisor.level must be one of ${LEVELS.join(", ")}`);
      const iv = a.interventions;
      need(
        Array.isArray(iv) && iv.every((i) => INTERVENTIONS.includes(i as Intervention)),
        `advisor.interventions must be a list of ${INTERVENTIONS.join(", ")}`,
      );
      need(int(a.max_consults, 0), "advisor.max_consults must be an integer >= 0");
      need(a.max_answer_tokens == null || int(a.max_answer_tokens, 1), "advisor.max_answer_tokens must be null or >= 1");
      need(a.answer_target_words == null || int(a.answer_target_words, 1), "advisor.answer_target_words must be null or >= 1");
      need(int(a.max_brief_tokens, 1), "advisor.max_brief_tokens must be an integer >= 1");
      need(int(a.field_target_words, 1), "advisor.field_target_words must be an integer >= 1");
      need(int(a.orient_after, 1), "advisor.orient_after must be an integer >= 1");
      const r = a.rules;
      need(
        obj(r) &&
          int(r.min_own_actions, 0) &&
          int(r.tool_cooldown_turns, 0) &&
          typeof r.require_hypothesis === "boolean" &&
          (r.max_advice_code_lines == null || int(r.max_advice_code_lines, 0)),
        "advisor.rules needs min_own_actions, tool_cooldown_turns >= 0, require_hypothesis, max_advice_code_lines",
      );
      need(int(a.cooldown_turns, 0), "advisor.cooldown_turns must be an integer >= 0");
      need(a.periodic_every == null || int(a.periodic_every, 1), "advisor.periodic_every must be null or >= 1");
      if (Array.isArray(iv) && iv.includes("periodic")) need(a.periodic_every != null, "the periodic intervention needs periodic_every");
      const s = a.stuck;
      need(
        obj(s) && int(s.repeat_calls, 1) && int(s.same_error, 1) && int(s.no_diff_turns, 1),
        "advisor.stuck needs repeat_calls, same_error, no_diff_turns >= 1",
      );
    }
  }
  const p = raw.prompts;
  if (p !== null && p !== undefined) {
    if (!obj(p) || !obj(p.texts)) {
      problems.push("prompts must be an object with texts");
    } else {
      need(typeof p.hash === "string", "prompts.hash must be a string");
      const texts = p.texts;
      for (const slot of SLOTS) need(typeof texts[slot] === "string", `prompts.texts.${slot} must be a string`);
    }
  }
  if (problems.length) throw new ConfigError(`${where}: ${problems.join("; ")}`);
  return {
    advisor_model: null,
    advisor: null,
    prompts: null,
    ...(raw as object),
  } as RunConfig;
}

export function isAdvisorArm(config: RunConfig): config is AdvisorRunConfig {
  return config.advisor !== null && config.advisor_model !== null && config.prompts !== null;
}

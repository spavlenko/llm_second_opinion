import { existsSync, readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";
import type { AdvisorRunConfig } from "../src/config.js";
import type { AdvisorSettings, ConsultRules, PromptSet, PromptTexts } from "../src/contracts.js";

const schema = (name: string) =>
  JSON.parse(readFileSync(new URL(`../../../schemas/${name}.schema.json`, import.meta.url), "utf8"));
// Pydantic emits OpenAPI's `discriminator`; ajv only needs to know the keyword exists.
const ajv = new Ajv2020({ strict: false });
export const validateEvent = ajv.compile(schema("event"));
export const validateRunConfigSchema = ajv.compile(schema("run-config"));

/** Consult rules that refuse nothing and cut no code. */
export const LOOSE: ConsultRules = { min_own_actions: 0, tool_cooldown_turns: 0, require_hypothesis: false, max_advice_code_lines: null };
/** The contract's default consult rules. */
export const DEFAULT_RULES: ConsultRules = { min_own_actions: 1, tool_cooldown_turns: 2, require_hypothesis: true, max_advice_code_lines: 5 };

/** Advisor settings as the harness writes them, with loose consult rules unless overridden. */
export function settings(overrides: Partial<AdvisorSettings> = {}): AdvisorSettings {
  return {
    level: "L2",
    interventions: ["plan", "consult", "stuck"],
    prompts: "default",
    max_consults: 5,
    max_answer_tokens: null,
    answer_target_words: 250,
    max_brief_tokens: 3000,
    field_target_words: 80,
    orient_after: 3,
    reserve_for_end: 1,
    clarify: false,
    memory: false,
    report_gate: false,
    surrogates: false,
    rules: LOOSE,
    cooldown_turns: 0,
    periodic_every: null,
    stuck: { repeat_calls: 3, same_error: 3, no_diff_turns: 8 },
    ...overrides,
  };
}

const endpoint = (model: string) => ({
  base_url: "http://127.0.0.1:9/v1",
  model,
  reasoning_effort: null,
  api_key_env: null,
  headers: {},
  header_env: {},
  temperature: null,
  top_p: null,
  sampling_seed: null,
});

const SLOTS = ["executor_guidance", "consult_tool", "brief", "advisor_system", "advice_injection"] as const;

/** The repository's prompt set `prompts/<name>/` as the harness writes it into advisor.json. */
export function promptSet(name = "default", partial = false): PromptSet {
  const read = (slot: string): [string, string][] => {
    const url = new URL(`../../../prompts/${name}/${slot}.md`, import.meta.url);
    if (partial && !existsSync(url)) return [];
    return [[slot, readFileSync(url, "utf8").trimEnd()]];
  };
  return {
    name,
    hash: "0123456789abcdef",
    texts: Object.fromEntries(SLOTS.flatMap(read)) as unknown as PromptTexts,
  };
}

export function runConfig(
  overrides: Partial<AdvisorSettings> = {},
  prompts: PromptSet = promptSet(),
): AdvisorRunConfig {
  return {
    schema_version: "1",
    run: { experiment: "e", arm: "H", task: "t", seed: 0, config_hash: "0123456789abcdef" },
    executor: endpoint("local"),
    advisor_model: endpoint("advisor"),
    advisor: settings(overrides),
    prompts,
    events_path: "/run/events.jsonl",
  };
}

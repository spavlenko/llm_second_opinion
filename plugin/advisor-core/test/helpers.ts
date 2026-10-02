import { existsSync, readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";
import type { AdvisorRunConfig } from "../src/config.js";
import type { AdvisorSettings, PromptSet, PromptTexts } from "../src/contracts.js";

const schema = (name: string) =>
  JSON.parse(readFileSync(new URL(`../../../schemas/${name}.schema.json`, import.meta.url), "utf8"));
// Pydantic emits OpenAPI's `discriminator`; ajv only needs to know the keyword exists.
const ajv = new Ajv2020({ strict: false });
export const validateEvent = ajv.compile(schema("event"));
export const validateRunConfigSchema = ajv.compile(schema("run-config"));

export function settings(overrides: Partial<AdvisorSettings> = {}): AdvisorSettings {
  return {
    level: "L2",
    interventions: ["plan", "consult", "stuck"],
    prompts: "default",
    max_consults: 5,
    max_answer_tokens: null,
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

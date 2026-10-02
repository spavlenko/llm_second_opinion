import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { ConfigError, isAdvisorArm, loadRunConfig, validateRunConfig } from "../src/config.js";
import { runConfig, validateRunConfigSchema } from "./helpers.js";

const prompts = {
  name: "baseline",
  hash: "0123456789abcdef",
  texts: { executor_guidance: "g", consult_tool: "c", brief: "b", advisor_system: "s", advice_injection: "{{advice}}" },
};

describe("run config", () => {
  it("the test fixture matches run-config.schema.json", () => {
    expect(validateRunConfigSchema(runConfig({}, prompts)), JSON.stringify(validateRunConfigSchema.errors)).toBe(true);
  });

  it("loads advisor.json from a file", () => {
    const path = join(mkdtempSync(join(tmpdir(), "cfg-")), "advisor.json");
    writeFileSync(path, JSON.stringify(runConfig({ level: "L1" }, prompts)));
    const config = loadRunConfig(path);
    expect(isAdvisorArm(config)).toBe(true);
    expect(config.advisor?.level).toBe("L1");
    expect(config.prompts?.texts.advice_injection).toBe("{{advice}}");
  });

  it("an arm without an advisor is valid and not an advisor arm", () => {
    const config = validateRunConfig({ ...runConfig(), advisor: null, advisor_model: null });
    expect(isAdvisorArm(config)).toBe(false);
  });

  it("reports every problem at once", () => {
    const bad = runConfig({ level: "L9" as never, interventions: ["plan", "nope" as never], max_consults: -1 });
    expect(() => validateRunConfig(bad)).toThrow(/advisor.level.*interventions.*max_consults/);
  });

  it("an advisor arm needs its prompt set", () => {
    expect(() => validateRunConfig({ ...runConfig(), prompts: null })).toThrow(/needs prompts/);
  });

  it("periodic needs periodic_every", () => {
    expect(() => validateRunConfig(runConfig({ interventions: ["periodic"] }))).toThrow(/periodic_every/);
  });

  it("rejects another schema version and incomplete prompts", () => {
    expect(() => validateRunConfig({ ...runConfig(), schema_version: "2" })).toThrow(ConfigError);
    const partial = { ...runConfig(), prompts: { name: "x", hash: "h", texts: { brief: "b" } } };
    expect(() => validateRunConfig(partial)).toThrow(/prompts.texts.executor_guidance/);
  });

  it("a missing or broken file is a ConfigError", () => {
    expect(() => loadRunConfig("/nonexistent/advisor.json")).toThrow(ConfigError);
  });
});

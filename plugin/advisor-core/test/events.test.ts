import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import { describe, expect, it } from "vitest";
import { EventWriter } from "../src/events.js";

const schema = JSON.parse(
  readFileSync(new URL("../../../schemas/event.schema.json", import.meta.url), "utf8"),
);
// Pydantic emits OpenAPI's `discriminator`; ajv only needs to know the keyword exists.
const validate = new Ajv2020({ strict: false }).compile(schema);

describe("EventWriter", () => {
  it("writes events that match event.schema.json", () => {
    const path = join(mkdtempSync(join(tmpdir(), "events-")), "events.jsonl");
    const writer = new EventWriter(path, () => 1700000000.5);
    writer.emit({ type: "trigger_fired", intervention: "stuck", reason: "same error x3", turn: 12 });
    writer.emit({ type: "brief_built", level: "L2", tokens: 800, identifiers_redacted: 4, role_map_size: 9 });
    writer.emit({ type: "advisor_request", request_id: "r1", input_tokens: 812, brief_text: "brief" });
    writer.emit({ type: "advisor_response", request_id: "r1", output_tokens: 300, cached_tokens: 0, latency_ms: 2100 });
    writer.emit({ type: "advice_applied", request_id: "r1", turn: 13 });
    writer.emit({ type: "budget_exhausted", consults_used: 5, limit: 5 });

    const lines = readFileSync(path, "utf8").trim().split("\n").map((l) => JSON.parse(l));
    expect(lines.map((e) => e.seq)).toEqual([0, 1, 2, 3, 4, 5]);
    for (const event of lines) {
      expect(validate(event), JSON.stringify(validate.errors)).toBe(true);
    }
  });

  it("the schema rejects a malformed event", () => {
    expect(validate({ schema_version: "1", seq: 0, ts: 1, type: "budget_exhausted", consults_used: 5 })).toBe(false);
  });
});

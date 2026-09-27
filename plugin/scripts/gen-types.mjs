// Generates advisor-core/src/contracts.ts from the JSON Schemas in ../schemas.
// Usage: node scripts/gen-types.mjs [--check]
import { readFileSync, readdirSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual } from "node:util";
import { compile } from "json-schema-to-typescript";

const here = dirname(fileURLToPath(import.meta.url));
const schemasDir = join(here, "..", "..", "schemas");
const outFile = join(here, "..", "advisor-core", "src", "contracts.ts");

// Merge every schema into one so shared definitions (Level, Intervention) are emitted once.
const defs = {};
const roots = {};
for (const file of readdirSync(schemasDir).filter((f) => f.endsWith(".schema.json")).sort()) {
  const { $defs = {}, $schema, title, ...root } = JSON.parse(readFileSync(join(schemasDir, file), "utf8"));
  for (const [name, def] of Object.entries({ ...$defs, [title]: { title, ...root } })) {
    if (name in defs && !isDeepStrictEqual(defs[name], def)) {
      throw new Error(`${file}: definition ${name} differs from an earlier schema`);
    }
    defs[name] = def;
  }
  roots[title] = { $ref: `#/$defs/${title}` };
}

const merged = {
  title: "Contracts",
  description: "Index of the shared contracts; use the named types below.",
  type: "object",
  properties: roots,
  additionalProperties: false,
  $defs: defs,
};

const ts = await compile(merged, "Contracts", {
  bannerComment:
    "/* Generated from schemas/*.schema.json by plugin/scripts/gen-types.mjs. Do not edit.\n" +
    " * Regenerate with `pnpm gen:types` after `bench schemas`. */",
  additionalProperties: false,
  unreachableDefinitions: true,
});

if (process.argv.includes("--check")) {
  if (!existsSync(outFile) || readFileSync(outFile, "utf8") !== ts) {
    console.error("advisor-core/src/contracts.ts is out of date; run `pnpm gen:types`");
    process.exit(1);
  }
  console.log("types are up to date");
} else {
  writeFileSync(outFile, ts);
  console.log(`wrote ${outFile}`);
}

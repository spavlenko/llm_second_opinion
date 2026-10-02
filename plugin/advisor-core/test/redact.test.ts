import { describe, expect, it } from "vitest";
import { Redactor, RoleMap, stripCodeBlocks, stripInlineCode } from "../src/redact.js";

describe("redaction", () => {
  it("prose: code-like tokens become role placeholders, words stay", () => {
    const r = new Redactor(new RoleMap());
    const out = r.prose(
      "Calling json::parse() on a JsonValue from include/nlohmann/json.hpp with parse_options and MAX_DEPTH " +
        "via getValue(x) and/or std::vector fails.",
    );
    expect(out).toBe(
      "Calling <function_1>() on a <type_1> from <file_1> with <variable_1> and <macro_1> " +
        "via <function_2>(x) and/or std::vector fails.",
    );
    expect(r.redacted).toBe(6);
  });

  it("prose: absolute paths and source file names", () => {
    const r = new Redactor(new RoleMap());
    expect(r.prose("see /testbed/src/a.cpp and b.hpp, not 1/2 or v3.1")).toBe("see <file_1> and <file_2>, not 1/2 or v3.1");
  });

  it("the same identifier gets the same placeholder across texts; restore maps back", () => {
    const roles = new RoleMap();
    const a = new Redactor(roles).prose("fix parse_options");
    const b = new Redactor(roles).prose("parse_options again, and dump_options");
    expect([a, b]).toEqual(["fix <variable_1>", "<variable_1> again, and <variable_2>"]);
    expect(roles.size).toBe(2);
    expect(roles.restore("Change <variable_2>, keep <variable_1>; <type_9> is unknown")).toBe(
      "Change dump_options, keep parse_options; <type_9> is unknown",
    );
  });

  it("code: every non-keyword name, with roles from the syntax", () => {
    const r = new Redactor(new RoleMap());
    const out = r.code('  if (lexer.get_token() == Token::Value && n > 0x1F) { return std::string("abc"); } // why');
    expect(out).toBe(
      '  if (<variable_1>.<function_1>() == <namespace_1>::<type_1> && n > 0x1F) { return std::string("…"); } // …',
    );
  });

  it("code: includes become file placeholders", () => {
    const r = new Redactor(new RoleMap());
    expect(r.code("#include <nlohmann/json.hpp>\n#include <vector>")).toBe("#include <<file_1>>\n#include <vector>");
  });

  it("inline code in prose is redacted as code", () => {
    const r = new Redactor(new RoleMap());
    expect(r.prose("`dump(indent)` breaks")).toBe("`<function_1>(<variable_1>)` breaks");
  });

  it("strips code blocks and multi-token inline code for the levels without code", () => {
    expect(stripCodeBlocks("a\n```cpp\nint x;\n```\nb")).toBe("a\n[code omitted]\nb");
    expect(stripInlineCode("call `foo()` or `a + b`")).toBe("call foo() or [code]");
  });
});

// Leaks found by the 2026-10-03 smoke run (docs/lab-notes.md): a name redacted in one part of
// a brief appeared raw in another, and the project name was never redacted.
describe("sweep", () => {
  it("replaces a known name wherever it appears raw", () => {
    const roles = new RoleMap();
    roles.placeholder("BENCHMARK", "macro");
    const { text, replaced } = roles.sweep("Rename #define BENCHMARK; grep <macro_1>( name )");
    expect(text).toBe("Rename #define <macro_1>; grep <macro_1>( name )");
    expect(replaced).toBe(1);
  });

  it("leaves short plain words and parts of longer names", () => {
    const roles = new RoleMap();
    roles.placeholder("name", "variable");
    roles.placeholder("parse", "variable");
    const { text } = roles.sweep("an unfortunate name; the parser calls parse_string");
    expect(text).toBe("an unfortunate name; the parser calls parse_string");
  });

  it("hides the project, case-insensitively, but not generic format names", () => {
    const roles = new RoleMap();
    roles.seedProject("simdjson__simdjson-524");
    expect(roles.sweep("consume simdjson via SIMDJSON headers").text).toBe("consume <project_1> via <project_1> headers");
    const json = new RoleMap();
    json.seedProject("nlohmann__json-18");
    expect(json.sweep("nlohmann's JSON parser").text).toBe("<project_1>'s JSON parser");
  });

  it("never rewrites inside a placeholder", () => {
    const roles = new RoleMap();
    roles.placeholder("macro_value", "variable");
    roles.placeholder("BIG", "macro");
    expect(roles.sweep("<macro_1> and macro_value").text).toBe("<macro_1> and <variable_1>");
  });
});

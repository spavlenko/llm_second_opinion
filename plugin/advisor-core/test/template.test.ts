import { describe, expect, it } from "vitest";
import { placeholders, renderTemplate } from "../src/template.js";

describe("templates", () => {
  it("renders known placeholders and leaves unknown ones empty", () => {
    const out = renderTemplate("Q: {{question}} / {{ tried }} / {{missing}}!", { question: "why?", tried: 2 });
    expect(out).toBe("Q: why? / 2 / !");
  });

  it("null and undefined render empty; braces that are not placeholders stay", () => {
    expect(renderTemplate("{{a}}{{b}} {x} {{ not-a-name }}", { a: null, b: undefined })).toBe(" {x} {{ not-a-name }}");
  });

  it("does not render placeholders inside values", () => {
    expect(renderTemplate("{{a}}", { a: "{{b}}", b: "no" })).toBe("{{b}}");
  });

  it("does not read inherited properties", () => {
    expect(renderTemplate("{{toString}}", {})).toBe("");
  });

  it("lists placeholders once, in order", () => {
    expect(placeholders("{{b}} {{a}} {{ b }}")).toEqual(["b", "a"]);
  });
});

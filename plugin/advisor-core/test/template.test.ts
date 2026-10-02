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

  it("a heading whose placeholder renders empty is dropped, with one blank line", () => {
    const t = "Intro.\n\nTried:\n{{tried}}\n\nError:\n{{error}}\n\nQuestion:\n{{question}}\n";
    expect(renderTemplate(t, { tried: "", error: "boom", question: "why?" })).toBe(
      "Intro.\n\nError:\nboom\n\nQuestion:\nwhy?\n",
    );
    expect(renderTemplate(t, { tried: "x", error: null, question: "   " })).toBe("Intro.\n\nTried:\nx\n");
    expect(renderTemplate("Tried:\n{{tried}}\n\nQ:\n{{q}}", { q: "?" })).toBe("Q:\n?");
  });

  it("a heading and an empty placeholder on one line are dropped", () => {
    expect(renderTemplate("A\nHypothesis: {{h}}\nB", {})).toBe("A\nB");
    expect(renderTemplate("A\nHypothesis: {{h}}\nB", { h: "none" })).toBe("A\nHypothesis: none\nB");
  });

  it("only headings go: a line that is not one, or has a placeholder, stays", () => {
    expect(renderTemplate("Hello\n{{a}}\nEnd", {})).toBe("Hello\n\nEnd");
    expect(renderTemplate("Left ({{n}}):\n{{a}}", { n: 2 })).toBe("Left (2):\n");
    expect(renderTemplate("{{a}} and {{b}}:", {})).toBe(" and :");
  });

  it("lists placeholders once, in order", () => {
    expect(placeholders("{{b}} {{a}} {{ b }}")).toEqual(["b", "a"]);
  });
});

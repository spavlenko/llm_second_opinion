// The brief builder: what the advisor learns about the task, at abstraction level L0–L3.
//
// L0  natural language only. No code, no error text, no test names. The task and the
//     executor's words with code blocks and inline code dropped, identifiers and file names as
//     role placeholders (<function_1>, <file_2>). Errors as a category ("The build fails.").
// L1  L0 plus error messages and failing test names, identifiers still as placeholders.
// L2  L1 plus short code excerpts (a few lines around the error locations, or the last edit),
//     every name in them as a placeholder; the task's code blocks are kept, redacted the same way.
// L3  verbatim: code excerpts, file paths, identifiers, error output, the task as written.
import type { Intervention, Level } from "./contracts.js";
import { errorLocations, errorText, testRun, type ToolObservation } from "./observe.js";
import { Redactor, type RoleMap, countPlaceholders, stripCodeBlocks, stripInlineCode } from "./redact.js";
import { placeholders, renderTemplate, type Vars } from "./template.js";

/** What a consult is about: who asked, why, and in the executor's own words if it asked. */
export interface BriefRequest {
  intervention: Intervention;
  reason: string;
  turn: number;
  question?: string;
  tried?: string;
  hypothesis?: string;
}

/** What the session has seen so far. */
export interface BriefContext {
  task: string;
  recent: ToolObservation[];
  lastFailure: ToolObservation | null;
  failedTests: string[];
  lastEdit: { path: string; text: string } | null;
  /** The executor's own words from its latest turns (its findings), for `orient`. */
  notes?: string[];
}

export interface Brief {
  text: string;
  tokens: number;
  identifiersRedacted: number;
  /** The brief was cut to max_brief_tokens. */
  truncated: boolean;
  vars: Vars;
}

/** Questions for harness triggers, where the executor did not write one. */
export const DEFAULT_QUESTIONS: Record<Intervention, string> = {
  plan: "How should I approach this task? Outline a plan.",
  consult: "What should I do next?", // the consult tool requires a question; only a fallback
  stuck: "I seem to be stuck. What should I try next?",
  on_test_failure: "The tests fail. What is the likely cause, and what should I check?",
  periodic: "Here is where I am. What should I do next?",
  orient:
    "I have looked around the code (what I did and found is above) and have not changed anything yet. " +
    "Am I looking in the right place, and what should I check before I change anything?",
  before_done: "I think I am done. Sanity-check my approach: is the fix in the right place, and what might I have missed?",
};

/** Marks the place where a brief over max_brief_tokens was cut. */
export const BRIEF_CUT_MARKER = "[cut: the brief was over its size limit]";

const EXCERPT_RADIUS = 3;
const MAX_EXCERPTS = 3;
const RECENT_ACTIONS = 6;

/** Rough token count (4 characters a token). Exposure is measured with it; cost comes from
 * the provider's usage through the metering proxy. */
export function approxTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

export function buildBrief(
  level: Level,
  template: string,
  req: BriefRequest,
  ctx: BriefContext,
  roles: RoleMap,
  readFile: (path: string) => string | null = () => null,
  maxTokens = Number.POSITIVE_INFINITY,
): Brief {
  const r = new Redactor(roles);
  const verbatim = level === "L3";
  // Prose the executor or the issue wrote: code dropped below L2, redacted at L2.
  const prose = (text: string | undefined): string => {
    if (!text) return "";
    if (verbatim) return text;
    if (level === "L2") {
      return text
        .split(/(```[^\n]*\n[\s\S]*?(?:```|$))/)
        .map((part, i) => {
          if (i % 2 === 0) return r.prose(part);
          const [fence, ...code] = part.split("\n");
          return [fence, r.code(code.join("\n"))].join("\n");
        })
        .join("");
    }
    return r.prose(stripInlineCode(stripCodeBlocks(text)));
  };
  const failure = ctx.lastFailure;

  // The `brief` slot's placeholders (docs/spec.md, Research design). Each is computed only if
  // the template uses it, so the role map holds only what was sent.
  const compute: Record<string, () => string | number> = {
    level: () => level,
    trigger: () => req.intervention,
    task_summary: () => prose(ctx.task),
    question: () => prose(req.question || DEFAULT_QUESTIONS[req.intervention]),
    // The executor's words when it asked; for a harness trigger, what it did lately.
    // For orient, also its notes: the findings the advisor is asked to check.
    tried: () => {
      if (req.intervention === "consult") return prose(req.tried);
      const actions = describeActions(level, ctx.recent, r);
      const notes = req.intervention === "orient" ? prose((ctx.notes ?? []).join("\n")).trim() : "";
      return notes ? `${actions}\nTheir notes so far:\n${notes}` : actions;
    },
    hypothesis: () => prose(req.hypothesis),
    error: () => {
      if (!failure) return "";
      if (level === "L0") return errorCategory(failure, ctx.failedTests.length);
      const raw = errorText(failure.result, level === "L1" ? 8 : 20);
      return verbatim ? raw : r.prose(raw);
    },
    code: () => (level === "L2" || verbatim ? excerpts(failure, ctx.lastEdit, readFile, verbatim, r) : ""),
  };
  const vars: Vars = {};
  for (const name of placeholders(template)) {
    if (Object.hasOwn(compute, name)) vars[name] = compute[name]!();
  }
  let text = renderTemplate(template, vars).replace(/\n{3,}/g, "\n\n").trim();
  // Names redacted in one section must not appear raw in another (or in the issue's prose).
  if (!verbatim) text = roles.sweep(text).text;
  // Cut after the sweep: a cut before it could leave part of a name the sweep no longer knows.
  const cut = cutBrief(text, maxTokens);
  text = cut.text;
  return { text, tokens: approxTokens(text), identifiersRedacted: countPlaceholders(text), truncated: cut.truncated, vars };
}

/** A brief over `maxTokens` (estimated), cut to fit: the longest section (blank-line separated
 * block) is cut at a line boundary, with the marker, and again until the brief fits, so the
 * headings and the short sections (the question) stay. A section without line breaks to cut
 * at is cut at a word. */
export function cutBrief(text: string, maxTokens: number): { text: string; truncated: boolean } {
  const limit = maxTokens * 4;
  if (text.length <= limit) return { text, truncated: false };
  const marked = `\n${BRIEF_CUT_MARKER}`;
  const sections = text.split(/\n{2,}/);
  const size = () => sections.join("\n\n").length;
  while (size() > limit) {
    let i = 0;
    sections.forEach((s, j) => {
      if (s.length > sections[i]!.length) i = j;
    });
    const section = sections[i]!;
    const body = section.endsWith(marked) ? section.slice(0, -marked.length) : section === BRIEF_CUT_MARKER ? "" : section;
    const room = limit - (size() - section.length) - marked.length;
    const kept = cutText(body, room);
    const next = kept ? kept + marked : BRIEF_CUT_MARKER;
    if (next.length >= section.length) break; // the longest section cannot shrink any more
    sections[i] = next;
  }
  let out = sections.join("\n\n");
  if (out.length > limit) out = (cutText(out, limit - marked.length) + marked).trim();
  return { text: out, truncated: true };
}

/** A prefix of `text` within `room` characters, ending at a line break if that keeps at least
 * half of it, or else at a space; empty when nothing fits. */
function cutText(text: string, room: number): string {
  if (room <= 0) return "";
  if (text.length <= room) return text;
  const head = text.slice(0, room);
  const line = head.lastIndexOf("\n");
  if (line >= room / 2) return head.slice(0, line).trimEnd();
  const space = head.lastIndexOf(" ");
  return (space > 0 ? head.slice(0, space) : head).trimEnd();
}

function errorCategory(failure: ToolObservation, failingTests: number): string {
  const run = testRun(failure);
  if (run?.buildFailed) return "The build fails.";
  if (run?.failed) return failingTests ? `${failingTests} test(s) fail.` : "The tests fail.";
  return failure.name === "bash" ? "A command fails." : `A ${failure.name} call fails.`;
}

/** The last tool calls in words. Below L2 commands are not shown, only what they did. */
export function describeActions(level: Level, recent: ToolObservation[], r: Redactor): string {
  const lines = recent.slice(-RECENT_ACTIONS).map((obs) => {
    const status = obs.isError ? " (failed)" : "";
    const path = typeof obs.args.path === "string" ? obs.args.path : null;
    const shown = (p: string) => (level === "L3" ? p : r.file(p));
    if (obs.name === "bash") {
      const run = testRun(obs);
      if (run) return `- ran the tests${run.failed ? " (failing)" : " (passing)"}`;
      const cmd = String(obs.args.command ?? "");
      if (level === "L3") return `- ran \`${cmd.slice(0, 200)}\`${status}`;
      if (level === "L2") return `- ran \`${r.prose(cmd.slice(0, 200))}\`${status}`;
      return `- ran a shell command${status}`;
    }
    const verb = { read: "read", edit: "edited", write: "wrote" }[obs.name] ?? `used ${obs.name} on`;
    return path ? `- ${verb} ${shown(path)}${status}` : `- used ${obs.name}${status}`;
  });
  return lines.join("\n");
}

function excerpts(
  failure: ToolObservation | null,
  lastEdit: BriefContext["lastEdit"],
  readFile: (path: string) => string | null,
  verbatim: boolean,
  r: Redactor,
): string {
  const header = (path: string, line?: number) => `${verbatim ? path : r.file(path)}${line ? `:${line}` : ""}`;
  const body = (code: string) => (verbatim ? code : r.code(code));
  const out: string[] = [];
  for (const loc of failure ? errorLocations(failure.result) : []) {
    if (out.length >= MAX_EXCERPTS) break;
    const content = readFile(loc.path);
    if (content === null) continue;
    const lines = content.split("\n");
    const from = Math.max(1, loc.line - EXCERPT_RADIUS);
    const to = Math.min(lines.length, loc.line + EXCERPT_RADIUS);
    if (from > to) continue;
    const width = String(to).length;
    const numbered = lines
      .slice(from - 1, to)
      .map((l, i) => `${String(from + i).padStart(width)} | ${body(l)}`)
      .join("\n");
    out.push(`${header(loc.path, loc.line)}\n${numbered}`);
  }
  if (!out.length && lastEdit) {
    const text = lastEdit.text.split("\n").slice(0, 2 * EXCERPT_RADIUS + 1).join("\n");
    out.push(`${header(lastEdit.path)} (last edit)\n${body(text)}`);
  }
  return out.join("\n\n");
}

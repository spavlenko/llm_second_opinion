// The `review` consult: one advisor call over an item's local candidate patches, which ranks
// them. The harness collects the candidates and their local checks; this builds the brief at
// the arm's level (L2 or L3: a diff is code) and reads the ranking from the answer.
import { abstractProse, approxTokens } from "./brief.js";
import type { AdvisorClientLike, Completion } from "./client.js";
import type { Level } from "./contracts.js";
import { Redactor, RoleMap } from "./redact.js";
import { renderTemplate } from "./template.js";

/** One candidate as the harness checked it locally. */
export interface ReviewCandidate {
  label: string;
  diff: string;
  builds: boolean;
  /** Visible tests that pass on the base commit and not with the patch. */
  broken: string[];
}

export interface ReviewRequest {
  level: Level;
  /** Task id: seeds the project name in the role map (below L3). */
  task: string;
  issue: string;
  candidates: ReviewCandidate[];
  templates: { system: string; brief: string };
  maxDiffTokens: number;
  maxAnswerTokens: number | null;
  requestId?: string;
}

export interface ReviewResult {
  system: string;
  brief: string;
  briefTokens: number;
  identifiersRedacted: number;
  /** The answer as received, null on an advisor error. */
  answer: string | null;
  /** Labels best first, names mapped back; empty when the answer has no valid ranking. */
  ranking: string[];
  pick: string | null;
  error: string | null;
  completion: Omit<Completion, "text"> | null;
  roleMap: Record<string, string>;
}

/** Shown in place of a diff's lines past max_diff_tokens. */
export const DIFF_CUT = (lines: number) => `[diff cut: ${lines} more lines]`;
/** Broken tests listed by name; the rest are counted. */
export const MAX_BROKEN_NAMES = 10;

/** A diff cut to about `maxTokens` at a line boundary. */
export function cutDiff(diff: string, maxTokens: number): string {
  const lines = diff.replace(/\n+$/, "").split("\n");
  let size = 0;
  for (let i = 0; i < lines.length; i++) {
    size += lines[i]!.length + 1;
    if (size > maxTokens * 4) return [...lines.slice(0, i), DIFF_CUT(lines.length - i)].join("\n");
  }
  return lines.join("\n");
}

/** A unified diff with file paths as placeholders and every line's code redacted as code;
 * `index` lines (blob hashes) are dropped. */
export function redactDiff(diff: string, r: Redactor): string {
  const out: string[] = [];
  for (const line of diff.split("\n")) {
    let m: RegExpExecArray | null;
    if ((m = /^diff --git a\/(\S+) b\/(\S+)$/.exec(line))) out.push(`diff --git a/${r.file(m[1]!)} b/${r.file(m[2]!)}`);
    else if (/^index [0-9a-f]+\.\.[0-9a-f]+/.test(line)) continue;
    else if ((m = /^(---|\+\+\+) (a\/|b\/)(\S+)$/.exec(line))) out.push(`${m[1]} ${m[2]}${r.file(m[3]!)}`);
    else if ((m = /^(@@ [^@]* @@)(.*)$/.exec(line))) out.push(m[1]! + r.code(m[2]!));
    else if (/^[ +-]/.test(line)) out.push(line[0] + r.code(line.slice(1)));
    else out.push(line.startsWith("[diff cut:") ? line : r.code(line));
  }
  return out.join("\n");
}

/** The labels of the last `RANKING:` line, in order, known labels only, each once. */
export function parseRanking(answer: string, labels: string[]): string[] {
  const line = answer
    .split("\n")
    .map((l) => l.replace(/[*_`]/g, "").trim())
    .reverse()
    .find((l) => /^RANKING\s*:/i.test(l));
  if (!line) return [];
  const known = new Set(labels);
  const out: string[] = [];
  for (const tok of line.replace(/^RANKING\s*:/i, "").split(/[>,]|\s+/)) {
    const label = tok.trim().replace(/^Candidate\s*/i, "").toUpperCase();
    if (known.has(label) && !out.includes(label)) out.push(label);
  }
  return out;
}

function candidateBlock(c: ReviewCandidate, diff: string): string {
  const n = c.broken.length;
  const names = c.broken.slice(0, MAX_BROKEN_NAMES).join(", ") + (n > MAX_BROKEN_NAMES ? `, … (${n - MAX_BROKEN_NAMES} more)` : "");
  return [
    `Candidate ${c.label}`,
    `Builds: ${c.builds ? "yes" : "no"}`,
    `Visible tests broken against the base commit: ${n ? `${n} (${names})` : "none"}`,
    "```diff",
    diff,
    "```",
  ].join("\n");
}

export function buildReviewBrief(req: ReviewRequest, roles: RoleMap): { system: string; brief: string; identifiersRedacted: number } {
  if (req.level !== "L2" && req.level !== "L3") throw new Error(`review needs level L2 or L3 (a diff is code), got ${req.level}`);
  const verbatim = req.level === "L3";
  const r = new Redactor(roles);
  if (!verbatim) roles.seedProject(req.task);
  const blocks = req.candidates.map((c) => {
    const diff = cutDiff(c.diff, req.maxDiffTokens);
    const shown = verbatim ? c : { ...c, broken: c.broken.map((t) => roles.placeholder(t, "test")) };
    return candidateBlock(shown, verbatim ? diff : redactDiff(diff, r));
  });
  const vars = {
    level: req.level,
    count: req.candidates.length,
    labels: req.candidates.map((c) => c.label).join(", "),
    issue: abstractProse(req.level, req.issue, r),
    candidates: blocks.join("\n\n"),
  };
  let brief = renderTemplate(req.templates.brief, vars).replace(/\n{3,}/g, "\n\n").trim();
  if (!verbatim) brief = roles.sweep(brief).text;
  const system = renderTemplate(req.templates.system, vars).trim();
  return { system, brief, identifiersRedacted: roles.count(brief) };
}

/** Build the brief, ask the advisor, read its ranking. Never throws for an advisor failure:
 * the error is in the result and the ranking is empty. */
export async function review(req: ReviewRequest, client: AdvisorClientLike): Promise<ReviewResult> {
  const roles = new RoleMap({ surrogates: false, seed: req.task });
  const { system, brief, identifiersRedacted } = buildReviewBrief(req, roles);
  const base = { system, brief, briefTokens: approxTokens(brief), identifiersRedacted };
  try {
    const done = await client.complete({ system, user: brief, maxTokens: req.maxAnswerTokens, requestId: req.requestId });
    const { text, ...completion } = done;
    const ranking = parseRanking(text, req.candidates.map((c) => c.label));
    return { ...base, answer: text, ranking, pick: ranking[0] ?? null, error: null, completion, roleMap: roles.toObject() };
  } catch (e) {
    const error = (e as Error).message || String(e);
    return { ...base, answer: null, ranking: [], pick: null, error, completion: null, roleMap: roles.toObject() };
  }
}

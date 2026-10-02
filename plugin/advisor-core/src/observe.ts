// What the trigger engine and the brief builder see of a run: an agent-neutral stream of
// observations that a binding (pi-binding) produces from its agent's events.

/** One tool call and its result, as the agent ran it. */
export interface ToolObservation {
  name: string;
  args: Record<string, unknown>;
  /** The result as text (what the model saw). */
  result: string;
  isError: boolean;
}

/** The task's test command (`eval_command` of every task image). */
export const TEST_COMMAND = "/opt/lso/run-tests";

const FILE_EDIT_TOOLS = new Set(["edit", "write"]);
// Shell commands that change files in place; a heuristic, so a `>` redirect does not count.
const BASH_EDIT = /\bsed\s+(-\w*\s+)*-i|\bperl\s+-\w*i|\bgit\s+(apply|checkout|restore|stash)\b|\bpatch\s+-/;

export interface TestRun {
  failed: boolean;
  /** Names of failing tests, when the output lists them. */
  failedTests: string[];
  buildFailed: boolean;
}

/** A bash call that ran the task's tests, and whether it failed. A run piped through `tail`
 * exits 0, so failure is also read from the ctest summary and run-tests' build message. */
export function testRun(obs: ToolObservation): TestRun | null {
  if (obs.name !== "bash" || !String(obs.args.command ?? "").includes(TEST_COMMAND)) return null;
  const out = obs.result;
  const buildFailed = out.includes("run-tests: build failed");
  const failedTests = failingTests(out);
  const summaryFailed = /\b[1-9]\d* tests? failed out of\b/.test(out) || /The following tests FAILED/.test(out);
  return { failed: obs.isError || buildFailed || summaryFailed || failedTests.length > 0, failedTests, buildFailed };
}

/** Test names from ctest's output ("The following tests FAILED:" block and per-test lines). */
export function failingTests(output: string): string[] {
  const names = new Set<string>();
  const block = output.split("The following tests FAILED:")[1];
  if (block) {
    for (const line of block.split("\n").slice(1)) {
      const m = /^\s*\d+\s+-\s+(\S+)/.exec(line);
      if (!m) break;
      names.add(m[1]!);
    }
  }
  for (const m of output.matchAll(/Test\s+#\d+:\s+(\S+)\s+\.*\*+(?:Failed|Exception|Timeout)/g)) names.add(m[1]!);
  return [...names];
}

/** Did the call change files? edit and write do; bash only by the in-place heuristic. */
export function editsFiles(obs: ToolObservation): boolean {
  if (obs.isError) return false;
  if (FILE_EDIT_TOOLS.has(obs.name)) return true;
  return obs.name === "bash" && BASH_EDIT.test(String(obs.args.command ?? ""));
}

const ERROR_LINE = /\berror\b|\bfailed\b|\bFAILED\b|\bassert|\bexception\b|\bundefined reference\b|\bnot found\b|\bfatal\b/i;
const EXIT_STATUS = /^Command exited with code \d+$/;

/** The error lines of a failed call's output (up to `max`), or its last lines when none look
 * like errors. */
export function errorText(output: string, max = 8): string {
  const lines = output
    .split("\n")
    .map((l) => l.trimEnd())
    .filter((l) => l.trim() && !EXIT_STATUS.test(l.trim()));
  const errors = [...new Set(lines.filter((l) => ERROR_LINE.test(l)))];
  // A ctest summary says "0 tests failed" when everything passed; that is not an error.
  const real = errors.filter((l) => !/\b0 tests failed\b/.test(l));
  return (real.length ? real.slice(0, max) : lines.slice(-3)).join("\n");
}

/** A failed call's error, normalized so that repeats compare equal: whitespace collapsed, and
 * numbers (line numbers, timings, counts) replaced, since they vary between otherwise
 * identical failures. */
export function errorSignature(output: string): string {
  return errorText(output, 3).replace(/\d+/g, "#").replace(/\s+/g, " ").trim();
}

/** Tool name plus arguments in a canonical form, for repeat detection. */
export function callSignature(obs: ToolObservation): string {
  return `${obs.name} ${canonicalJson(obs.args)}`;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/** `path:line` locations in compiler and test output (gcc, clang, Catch2, doctest). */
export function errorLocations(output: string): { path: string; line: number }[] {
  const seen = new Set<string>();
  const out: { path: string; line: number }[] = [];
  for (const m of output.matchAll(/(?:^|[\s(])((?:\/|\.\/)?[\w./+-]+\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx|ipp|inl|tpp))[:(](\d+)/gm)) {
    const key = `${m[1]}:${m[2]}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ path: m[1]!, line: Number(m[2]) });
  }
  return out;
}

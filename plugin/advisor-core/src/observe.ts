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

// Words that may come before the command itself: `timeout 600`, `bash`, `env A=b`, `time`.
const COMMAND_PREFIX = /^(?:(?:timeout\s+(?:-\S+\s+)*\S+|bash|sh|time|env|nice|stdbuf\s+\S+|[A-Za-z_]\w*=\S*)\s+)*/;

/** Does the shell command run the test script (as a command, not as an argument to `cat`,
 * `sed` or `grep`)? */
export function runsTests(command: string): boolean {
  return command
    .split(/&&|\|\||[;|\n(){}]/)
    .some((segment) => segment.trim().replace(COMMAND_PREFIX, "").split(/\s+/)[0] === TEST_COMMAND);
}

/** A bash call that ran the task's tests, and whether it failed. A run piped through `tail`
 * exits 0, so failure is also read from the ctest summary and run-tests' build message. */
export function testRun(obs: ToolObservation): TestRun | null {
  if (obs.name !== "bash" || !runsTests(String(obs.args.command ?? ""))) return null;
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

/** A failure worth showing as the latest build or test output: a failed test run, or a failed
 * shell command that was not just looking (a compile, a repro binary). A grep that matches
 * nothing or a missing file is not one. */
export function buildOrTestFailure(obs: ToolObservation): boolean {
  if (testRun(obs)?.failed) return true;
  return obs.name === "bash" && obs.isError && !readsCode(obs);
}

/** Did the call change files? edit and write do; bash only by the in-place heuristic. */
export function editsFiles(obs: ToolObservation): boolean {
  if (obs.isError) return false;
  if (FILE_EDIT_TOOLS.has(obs.name)) return true;
  return obs.name === "bash" && BASH_EDIT.test(String(obs.args.command ?? ""));
}

const READ_TOOLS = new Set(["read", "grep", "find", "ls"]);
// Shell commands that only look: the first word of the command (after any `cd dir &&`).
const READ_COMMANDS = new Set(["cat", "head", "tail", "less", "more", "grep", "egrep", "rg", "ag", "ls", "find", "tree", "wc", "nl", "sed", "awk", "file", "stat"]);
const GIT_READ = new Set(["log", "show", "diff", "grep", "status", "blame", "ls-files"]);

/** Did the call only look at the code? The read-type tools, and shell commands that read
 * (cat, grep, ls, find, git log, ...), not the tests and not an in-place edit. */
export function readsCode(obs: Pick<ToolObservation, "name" | "args">): boolean {
  if (READ_TOOLS.has(obs.name)) return true;
  if (obs.name !== "bash") return false;
  const command = String(obs.args.command ?? "");
  if (runsTests(command) || BASH_EDIT.test(command)) return false;
  const words = command.replace(/^\s*(cd\s+\S+\s*(&&|;)\s*)+/, "").trim().split(/\s+/);
  if (words[0] === "git") return GIT_READ.has(words.find((w, i) => i > 0 && !w.startsWith("-")) ?? "");
  return READ_COMMANDS.has(words[0] ?? "");
}

// Shell commands that print files, and which of their options take a separate value (`-n 20`).
const FILE_COMMANDS: Record<string, { valueOptions: string; patternFirst: boolean }> = {
  cat: { valueOptions: "", patternFirst: false },
  nl: { valueOptions: "", patternFirst: false },
  less: { valueOptions: "", patternFirst: false },
  more: { valueOptions: "", patternFirst: false },
  head: { valueOptions: "nc", patternFirst: false },
  tail: { valueOptions: "nc", patternFirst: false },
  sed: { valueOptions: "ef", patternFirst: true },
  awk: { valueOptions: "fvF", patternFirst: true },
  grep: { valueOptions: "efABCm", patternFirst: true },
  egrep: { valueOptions: "efABCm", patternFirst: true },
  rg: { valueOptions: "efABCmgt", patternFirst: true },
};
// A word that names a file: a name with an extension, no glob or redirect characters.
const FILE_WORD = /^[\w./+-]*[\w+-]\.[A-Za-z][\w+]*$/;

/** The files a call read, where that can be told: the read tool's path, and the file arguments
 * of `cat`, `head`, `tail`, `sed -n`, `grep`, `rg`, `awk` (and the like) in shell commands,
 * normalized (`./` dropped). Directories, globs and patterns are not files read. */
export function filesRead(obs: Pick<ToolObservation, "name" | "args">): string[] {
  const norm = (p: string) => p.replace(/^(\.\/)+/, "");
  if (obs.name === "read") return typeof obs.args.path === "string" ? [norm(obs.args.path)] : [];
  if (obs.name !== "bash") return [];
  const command = String(obs.args.command ?? "");
  if (runsTests(command) || BASH_EDIT.test(command)) return [];
  const out = new Set<string>();
  for (const segment of command.split(/&&|\|\||[;|\n]/)) {
    const words = shellWords(segment);
    const spec = FILE_COMMANDS[words[0] ?? ""];
    if (!spec) continue;
    const positional: string[] = [];
    let explicitPattern = false;
    for (let i = 1; i < words.length; i++) {
      const w = words[i]!;
      if (/^\d*[<>]/.test(w)) {
        if (/^\d*[<>]+&?$/.test(w)) i++; // `> file`: the target is not read
        continue;
      }
      if (w.startsWith("-") && w.length > 1) {
        // Only the last letter of a short-option group can take the next word as its value.
        const flag = w.startsWith("--") ? "" : w.slice(-1);
        const takesValue = flag !== "" && spec.valueOptions.includes(flag) && !/\d/.test(w.slice(1));
        if (takesValue && (flag === "e" || flag === "f")) explicitPattern = true; // -e PATTERN, -f SCRIPT
        if (takesValue) i++;
        continue;
      }
      positional.push(w);
    }
    const files = spec.patternFirst && !explicitPattern ? positional.slice(1) : positional;
    for (const f of files) if (FILE_WORD.test(f) && !f.startsWith("/dev/")) out.add(norm(f));
  }
  return [...out];
}

/** Words of a simple shell command, with quotes removed (no expansion). */
function shellWords(command: string): string[] {
  const words: string[] = [];
  for (const m of command.trim().matchAll(/"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+)/g)) words.push(m[1] ?? m[2] ?? m[3]!);
  return words;
}

const ERROR_LINE =/\berror\b|\bfailed\b|\bFAILED\b|\bassert|\bexception\b|\bundefined reference\b|\bnot found\b|\bfatal\b/i;
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

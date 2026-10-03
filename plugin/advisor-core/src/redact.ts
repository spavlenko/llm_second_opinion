// Identifier redaction for briefs below L3. Identifiers are replaced by role placeholders
// (`<function_1>`, `<type_2>`, `<file_1>`, ...) from a role map that lives for the whole session,
// so the same identifier always gets the same placeholder and advice that mentions a
// placeholder can be mapped back before it reaches the executor.
//
// Two modes:
// - prose (issue text, the executor's question, error messages): only code-like tokens are
//   identifiers: qualified names (a::b), calls (f(), f(x)), snake_case, camelCase, PascalCase
//   with two humps or more, ALL_CAPS_WITH_UNDERSCORES, file paths and source file names.
//   Ordinary words stay.
// - code (excerpts): every name that is not a C++ keyword or a well-known standard name.
// Names in the `std` namespace are public and kept in both modes.

export type Role = "function" | "type" | "macro" | "namespace" | "variable" | "file" | "test" | "identifier" | "project";

const PLACEHOLDER = /<(function|type|macro|namespace|variable|file|test|identifier|project)_(\d+)>/g;

const KEYWORDS = new Set(
  (
    "alignas alignof and asm auto bool break case catch char char8_t char16_t char32_t class const consteval " +
    "constexpr constinit const_cast continue co_await co_return co_yield decltype default delete do double " +
    "dynamic_cast else enum explicit export extern false float for friend goto if inline int long mutable " +
    "namespace new noexcept not nullptr operator or private protected public register reinterpret_cast " +
    "requires return short signed sizeof static static_assert static_cast struct switch template this " +
    "thread_local throw true try typedef typeid typename union unsigned using virtual void volatile wchar_t " +
    "while override final include define ifdef ifndef endif elif pragma undef defined NULL main " +
    "std size_t ssize_t ptrdiff_t nullptr_t int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t " +
    "uintptr_t intptr_t string string_view vector map set unordered_map unordered_set array pair tuple " +
    "optional variant unique_ptr shared_ptr weak_ptr make_unique make_shared move forward swap begin end " +
    "size empty push_back emplace_back cout cerr endl printf assert abs min max"
  ).split(" "),
);

// Repository names that are public format or protocol names, not project identity (json in
// nlohmann/json): redacting them would hide what the code is about, not who wrote it.
const GENERIC_PROJECT_WORDS = new Set(["json", "xml", "yaml", "toml", "csv", "http", "sql", "regex"]);

const SOURCE_EXT = "c|cc|cpp|cxx|h|hh|hpp|hxx|ipp|inl|tpp|txt|cmake|py|sh|json|yaml|yml|md";
// Order matters: the first alternative that matches wins.
const PROSE_TOKEN = new RegExp(
  [
    // absolute paths, relative paths with a source extension, relative paths of 3+ parts
    `(?<file>(?<![\\w.])/(?:[\\w.+-]+/)*[\\w.+-]*[A-Za-z][\\w.+-]*|\\b(?:[\\w.+-]+/)*[\\w+-]+\\.(?:${SOURCE_EXT})\\b|\\b[\\w.+-]+(?:/[\\w.+-]+){2,})`,
    // a single name in quotes, as compilers quote identifiers ('Serializer', ‘x’)
    `(?<quoted>(?<=['‘"])~?[A-Za-z_][\\w:~]*(?=['’"]))`,
    `(?<qualified>\\b(?:[A-Za-z_]\\w*)?(?:::~?[A-Za-z_]\\w*)+(?:\\(\\))?)`,
    `(?<call>\\b[A-Za-z_]\\w*\\(\\))`,
    `(?<callargs>\\b[A-Za-z_]\\w*(?=\\())`,
    `(?<snake>\\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\\b|\\b_[A-Za-z0-9_]+\\b)`,
    `(?<camel>\\b[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*\\b)`,
    `(?<pascal>\\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\\b)`,
  ].join("|"),
  "g",
);
const CODE_TOKEN = /\b\d[\w.']*|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)'|\/\/[^\n]*|<[\w./+-]+\.(?:h|hpp|hh|hxx|ipp|inl)>|[A-Za-z_]\w*/g;

/** Placeholders in a text: the identifiers a brief redacted. */
export function countPlaceholders(text: string): number {
  return [...text.matchAll(PLACEHOLDER)].length;
}

// Word lists for surrogates: plausible names that say nothing about the real ones.
const VERBS = ["compute", "update", "load", "build", "handle", "process", "apply", "resolve", "fetch", "emit", "check", "convert"];
const NOUNS = ["value", "item", "node", "entry", "record", "buffer", "state", "token", "field", "block", "frame", "chunk"];
const TYPES = ["Widget", "Gadget", "Element", "Holder", "Context", "Handler", "Packet", "Unit", "Slot", "Cell"];
const SOURCE_FILE_EXT = /\.(c|cc|cpp|cxx|h|hh|hpp|hxx|ipp|inl|tpp|py|sh|cmake|txt)$/;

/** A small deterministic PRNG (mulberry32) seeded by a string (FNV-1a). */
function seededRandom(seed: string): () => number {
  let h = 0x811c9dc5;
  for (let i = 0; i < seed.length; i++) h = Math.imul(h ^ seed.charCodeAt(i), 0x01000193);
  let a = h >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");

export interface RoleMapOptions {
  /** Plausible fake names (`compute_value_3`, `Widget7`, `src/module_2.cpp`) instead of
   * `<role_N>` placeholders. */
  surrogates?: boolean;
  /** Seeds the surrogate names, so a run always gets the same ones (the run's identity). */
  seed?: string;
}

export class RoleMap {
  private byName = new Map<string, string>();
  private byPlaceholder = new Map<string, string>();
  private counts = new Map<Role, number>();
  private projects = new Set<string>();
  readonly surrogates: boolean;
  private readonly random: () => number;
  /** Words seen in the run's text (the task, tool output, the executor's words): a surrogate
   * must not be one of them. */
  private seen = new Set<string>();
  private surrogatePattern: RegExp | null = null;

  constructor(options: RoleMapOptions = {}) {
    this.surrogates = options.surrogates ?? false;
    this.random = seededRandom(options.seed ?? "");
  }

  get size(): number {
    return this.byName.size;
  }

  /** Records the words of a text the run has seen, so no surrogate collides with a real name. */
  noteText(text: string): void {
    if (!this.surrogates) return;
    for (const [w] of text.matchAll(/[\w./+-]+/g)) {
      this.seen.add(w);
      for (const part of w.split(/[./+-]+/)) if (part) this.seen.add(part);
    }
  }

  placeholder(name: string, role: Role): string {
    let p = this.byName.get(name);
    if (!p) {
      const n = (this.counts.get(role) ?? 0) + 1;
      this.counts.set(role, n);
      p = this.surrogates ? this.surrogate(name, role, n) : `<${role}_${n}>`;
      this.byName.set(name, p);
      this.byPlaceholder.set(p, name);
      this.surrogatePattern = null;
    }
    return p;
  }

  private surrogate(name: string, role: Role, n: number): string {
    const pick = <T>(list: readonly T[]): T => list[Math.floor(this.random() * list.length)]!;
    const taken = (s: string) =>
      this.byPlaceholder.has(s) || this.byName.has(s) || this.seen.has(s) || this.seen.has(s.split("/").at(-1)!);
    for (let k = n; ; k++) {
      const noun = pick(NOUNS);
      const s = {
        function: () => `${pick(VERBS)}_${noun}_${k}`,
        type: () => `${pick(TYPES)}${k}`,
        macro: () => `${noun.toUpperCase()}_FLAG_${k}`,
        namespace: () => `${noun}_ns${k}`,
        variable: () => `${noun}_${k}`,
        file: () => `src/module_${k}${SOURCE_FILE_EXT.exec(name)?.[0] ?? ""}`,
        test: () => `test_${noun}_${k}`,
        identifier: () => `${noun}_ref_${k}`,
        project: () => `${noun}lib${k}`,
      }[role]();
      if (!taken(s)) return s;
    }
  }

  /** Matches every surrogate in use, and a file surrogate's base name, as whole names. */
  private surrogateRegex(): RegExp | null {
    if (!this.surrogates || !this.byPlaceholder.size) return null;
    if (!this.surrogatePattern) {
      const forms = [...this.byPlaceholder.keys()].flatMap((s) => (s.includes("/") ? [s, s.split("/").at(-1)!] : [s]));
      const sorted = [...new Set(forms)].sort((a, b) => b.length - a.length).map(escapeRe);
      this.surrogatePattern = new RegExp(`(?<!\\w)(?:${sorted.join("|")})(?![\\w/])`, "g");
    }
    this.surrogatePattern.lastIndex = 0;
    return this.surrogatePattern;
  }

  /** The real name a surrogate (or a file surrogate's base name) stands for. */
  private nameOf(token: string): string | undefined {
    const name = this.byPlaceholder.get(token);
    if (name !== undefined) return name;
    for (const [s, real] of this.byPlaceholder) if (s.includes("/") && s.split("/").at(-1) === token) return real.split("/").at(-1);
    return undefined;
  }

  /** Placeholders (or surrogates) in `text` replaced by the names they stand for; unknown ones
   * stay. */
  restore(text: string): string {
    const pattern = this.surrogateRegex();
    if (pattern) return text.replace(pattern, (s) => this.nameOf(s) ?? s);
    return text.replace(PLACEHOLDER, (p) => this.byPlaceholder.get(p) ?? p);
  }

  /** [name, placeholder] pairs, in the order the placeholders were assigned. */
  entries(): [string, string][] {
    return [...this.byName.entries()];
  }

  /** The placeholders (or surrogates) in a text, with repeats. */
  private found(text: string): string[] {
    const pattern = this.surrogateRegex();
    if (pattern) return [...text.matchAll(pattern)].map((m) => m[0]).filter((s) => this.byPlaceholder.has(s));
    if (this.surrogates) return [];
    return [...text.matchAll(PLACEHOLDER)].map((m) => m[0]);
  }

  /** How many identifiers a text carries as placeholders (or surrogates). */
  count(text: string): number {
    return this.found(text).length;
  }

  /** Placeholder (or surrogate) to name for every known one that appears in `text`. */
  mapFor(text: string): Record<string, string> {
    const out: Record<string, string> = {};
    for (const p of this.found(text)) {
      const name = this.byPlaceholder.get(p);
      if (name !== undefined) out[p] = name;
    }
    return out;
  }

  /** The task's organisation and repository names (from an id like `org__repo-123`), so the
   * final sweep hides which project the brief is about. Matched case-insensitively. */
  seedProject(taskId: string): void {
    const m = /^([\w.-]+?)__([\w.-]+?)(?:-\d+)?$/.exec(taskId);
    if (!m) return;
    for (const name of new Set([m[1]!, m[2]!])) {
      if (name.length >= 3 && !GENERIC_PROJECT_WORDS.has(name.toLowerCase())) {
        this.placeholder(name, "project");
        this.projects.add(name.toLowerCase());
      }
    }
  }

  /** Every known name left verbatim in `text` replaced by its placeholder, so a name redacted
   * in one part of a brief cannot appear raw in another. Matches whole names only (not inside
   * longer identifiers, not inside placeholders). Plain lowercase words under 8 letters that the
   * map learned from code (`name`, `parse`) are left, to keep prose readable; the leakage
   * scorer measures what that lets through. Returns the text and the number of replacements. */
  sweep(text: string): { text: string; replaced: number } {
    const names = [...this.byName.keys()]
      .filter((n) => this.projects.has(n.toLowerCase()) || !/^[a-z]{1,7}$/.test(n))
      .sort((a, b) => b.length - a.length);
    if (!names.length) return { text, replaced: 0 };
    const escaped = names.map((n) => n.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&"));
    const pattern = new RegExp(`(?<![\\w/.])(?:${escaped.join("|")})(?![\\w/])`, "gi");
    let replaced = 0;
    // Placeholders (or surrogates) already in the text are left alone.
    const surrogate = this.surrogateRegex();
    const protect = surrogate
      ? new RegExp(`(${surrogate.source})`)
      : /(<(?:function|type|macro|namespace|variable|file|test|identifier|project)_\d+>)/;
    const out = text
      .split(protect)
      .map((part, i) =>
        i % 2 === 1
          ? part
          : part.replace(pattern, (match) => {
              const key = this.byName.has(match) ? match : names.find((n) => n.toLowerCase() === match.toLowerCase());
              if (!key) return match;
              // Case-insensitive only for project names; identifiers are case-sensitive.
              if (key !== match && !this.projects.has(key.toLowerCase())) return match;
              replaced += 1;
              return this.byName.get(key)!;
            }),
      )
      .join("");
    return { text: out, replaced };
  }

  /** Placeholder to name for the whole map so far. */
  toObject(): Record<string, string> {
    return Object.fromEntries([...this.byName.entries()].map(([name, p]) => [p, name]));
  }
}

/** Redacts text into a role map, counting the identifiers it replaces. */
export class Redactor {
  redacted = 0;

  constructor(readonly roles: RoleMap) {}

  private replace(name: string, role: Role): string {
    this.redacted += 1;
    return this.roles.placeholder(name, role);
  }

  /** A name known to be a file path. */
  file(path: string): string {
    return this.replace(path, "file");
  }

  /** Code-like tokens in prose. Inline `code` spans are redacted as code. */
  prose(text: string): string {
    return text
      .split(/(`[^`\n]+`)/)
      .map((part, i) => (i % 2 === 1 ? `\`${this.code(part.slice(1, -1))}\`` : this.proseOnly(part)))
      .join("");
  }

  private proseOnly(text: string): string {
    return text.replace(PROSE_TOKEN, (match, ...rest) => {
      const g = rest.at(-1) as Record<string, string | undefined>;
      if (isPublic(match)) return match;
      if (g.file) return this.replace(match, "file");
      if (g.quoted) return KEYWORDS.has(match) || match.length <= 1 ? match : this.replace(match, roleOfName(match));
      if (g.qualified || g.call) return this.replace(match.replace(/\(\)$/, ""), roleOfQualified(match)) + (match.endsWith("()") ? "()" : "");
      if (g.callargs) return KEYWORDS.has(match) ? match : this.replace(match, "function");
      if (g.snake) return this.replace(match, /^[A-Z0-9_]+$/.test(match) ? "macro" : "variable");
      if (g.camel) return this.replace(match, "variable");
      if (g.pascal) return this.replace(match, "type");
      return match;
    });
  }

  /** Every non-keyword name in a code excerpt; string literals and comments are kept as
   * their shape only. */
  code(text: string): string {
    return text.replace(CODE_TOKEN, (tok, offset: number, all: string) => {
      if (/^\d/.test(tok)) return tok;
      if (tok.startsWith('"')) return tok.length > 2 ? '"…"' : tok;
      if (tok.startsWith("'")) return tok;
      if (tok.startsWith("//")) return "// …";
      if (tok.startsWith("<")) return `<${this.replace(tok.slice(1, -1), "file")}>`;
      if (KEYWORDS.has(tok) || tok.length <= 1 || isStdQualified(all, offset)) return tok;
      const next = all.slice(offset + tok.length).match(/^\s*(\(|::)/)?.[1];
      const role: Role =
        next === "::" ? "namespace" : next === "(" ? "function" : /^[A-Z][A-Z0-9_]+$/.test(tok) ? "macro" : /^[A-Z]/.test(tok) ? "type" : "variable";
      return this.replace(tok, role);
    });
  }

}

function isPublic(token: string): boolean {
  return /^(?:::)?std::/.test(token);
}

/** Is the name at `offset` qualified by `std::` (e.g. `vector` in `std::vector`)? */
function isStdQualified(all: string, offset: number): boolean {
  return /\bstd\s*::\s*$/.test(all.slice(Math.max(0, offset - 8), offset));
}

function roleOfName(name: string): Role {
  if (name.includes("::")) return roleOfQualified(name);
  return /^[A-Z][A-Z0-9_]+$/.test(name) ? "macro" : /^[A-Z]/.test(name) ? "type" : "variable";
}

function roleOfQualified(name: string): Role {
  const last = name.replace(/\(\)$/, "").split("::").at(-1) ?? name;
  if (name.endsWith("()")) return "function";
  if (/^~/.test(last)) return "function";
  if (/^[A-Z][A-Z0-9_]+$/.test(last)) return "macro";
  return /^[A-Z]/.test(last) ? "type" : "identifier";
}

/** Fenced code blocks (```) replaced by a marker, for levels that send no code. */
export function stripCodeBlocks(text: string, marker = "[code omitted]"): string {
  return text.replace(/```[\s\S]*?(?:```|$)/g, marker);
}

/** Inline code spans replaced by a marker, for levels that send no code. A span holding one
 * name is kept (as a name), so prose redaction can turn it into a placeholder. */
export function stripInlineCode(text: string, marker = "[code]"): string {
  return text.replace(/`([^`\n]+)`/g, (_, inner: string) => (/^[\w:~.\/-]+(\(\))?$/.test(inner.trim()) ? inner.trim() : marker));
}

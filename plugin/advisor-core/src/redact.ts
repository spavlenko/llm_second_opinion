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

export type Role = "function" | "type" | "macro" | "namespace" | "variable" | "file" | "test" | "identifier";

const PLACEHOLDER = /<(function|type|macro|namespace|variable|file|test|identifier)_(\d+)>/g;

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

export class RoleMap {
  private byName = new Map<string, string>();
  private byPlaceholder = new Map<string, string>();
  private counts = new Map<Role, number>();

  get size(): number {
    return this.byName.size;
  }

  placeholder(name: string, role: Role): string {
    let p = this.byName.get(name);
    if (!p) {
      const n = (this.counts.get(role) ?? 0) + 1;
      this.counts.set(role, n);
      p = `<${role}_${n}>`;
      this.byName.set(name, p);
      this.byPlaceholder.set(p, name);
    }
    return p;
  }

  /** Placeholders in `text` replaced by the names they stand for; unknown ones stay. */
  restore(text: string): string {
    return text.replace(PLACEHOLDER, (p) => this.byPlaceholder.get(p) ?? p);
  }

  entries(): [string, string][] {
    return [...this.byName.entries()];
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

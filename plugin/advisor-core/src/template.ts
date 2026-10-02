// `{{name}}` templates for the prompt slots. Unknown names render empty: the harness checks
// placeholders against each slot's list when it loads a prompt set, so here a missing value
// only means "nothing to say".
//
// A section with nothing in it is dropped: a heading line (text ending in ":", no placeholder)
// whose content is a line holding only a placeholder that renders empty, or a line that is such
// a heading followed by the empty placeholder ("Hypothesis: {{hypothesis}}"). One blank line
// around the dropped section goes with it, so no gap is left behind.

const PLACEHOLDER = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;
const ONLY_PLACEHOLDER = /^\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*$/;
const HEADING_AND_PLACEHOLDER = /^([^{}]*:)\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*$/;

export type Vars = Record<string, string | number | null | undefined>;

function value(vars: Vars, name: string): string {
  const v = Object.hasOwn(vars, name) ? vars[name] : undefined;
  return v === null || v === undefined ? "" : String(v);
}

function isHeading(line: string): boolean {
  return /\S.*:\s*$/.test(line) && !/\{\{/.test(line);
}

const blank = (line: string | undefined) => line !== undefined && line.trim() === "";

export function renderTemplate(template: string, vars: Vars): string {
  const lines = template.split("\n");
  const kept: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    const only = ONLY_PLACEHOLDER.exec(line);
    const inline = HEADING_AND_PLACEHOLDER.exec(line);
    const emptySection =
      (only && kept.length > 0 && isHeading(kept.at(-1)!) && value(vars, only[1]!).trim() === "") ||
      (inline && value(vars, inline[2]!).trim() === "");
    if (!emptySection) {
      kept.push(line);
      continue;
    }
    if (only) kept.pop(); // the heading
    // Drop one blank line next to the section: the one after it, or else the one before it.
    if (blank(lines[i + 1]) && (kept.length === 0 || blank(kept.at(-1)))) i++;
    else if (blank(kept.at(-1)) && (i + 1 >= lines.length || blank(lines[i + 1]))) kept.pop();
  }
  return kept.join("\n").replace(PLACEHOLDER, (_, name: string) => value(vars, name));
}

/** The placeholder names a template uses, in order of first use. */
export function placeholders(template: string): string[] {
  return [...new Set([...template.matchAll(PLACEHOLDER)].map((m) => m[1]!))];
}

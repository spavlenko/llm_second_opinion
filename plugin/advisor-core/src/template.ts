// `{{name}}` templates for the prompt slots. Unknown names render empty: the harness checks
// placeholders against each slot's list when it loads a prompt set, so here a missing value
// only means "nothing to say".

const PLACEHOLDER = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;

export type Vars = Record<string, string | number | null | undefined>;

export function renderTemplate(template: string, vars: Vars): string {
  return template.replace(PLACEHOLDER, (_, name: string) => {
    const value = Object.hasOwn(vars, name) ? vars[name] : undefined;
    return value === null || value === undefined ? "" : String(value);
  });
}

/** The placeholder names a template uses, in order of first use. */
export function placeholders(template: string): string[] {
  return [...new Set([...template.matchAll(PLACEHOLDER)].map((m) => m[1]!))];
}

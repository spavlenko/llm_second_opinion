// What the advice may carry back to the executor: `max_advice_code_lines` keeps the advisor to
// hints, so the executor still writes the fix.

/** Follows a fenced code block that was cut, so the executor knows why. */
export const CODE_CUT_NOTE = "[code shortened: the advisor gives hints, you write the fix]";

const FENCE = /^\s*(`{3,}|~{3,})/;

/** Fenced code blocks longer than `maxLines` lines cut to their first `maxLines` (0 removes the
 * block, fences and all), each followed by CODE_CUT_NOTE; null leaves the text alone. A block
 * left open (an answer cut off mid-block) runs to the end. Inline code spans stay. Returns the
 * text and the number of code lines removed. */
export function limitCodeBlocks(text: string, maxLines: number | null): { text: string; removed: number } {
  if (maxLines === null) return { text, removed: 0 };
  const lines = text.split("\n");
  const out: string[] = [];
  let removed = 0;
  for (let i = 0; i < lines.length; i++) {
    const open = FENCE.exec(lines[i]!);
    if (!open) {
      out.push(lines[i]!);
      continue;
    }
    const fence = open[1]!;
    let end = i + 1;
    while (end < lines.length && !lines[end]!.trim().startsWith(fence)) end++;
    const body = lines.slice(i + 1, end);
    const closed = end < lines.length;
    if (body.length <= maxLines) {
      out.push(...lines.slice(i, closed ? end + 1 : end));
    } else {
      removed += body.length - maxLines;
      if (maxLines > 0) out.push(lines[i]!, ...body.slice(0, maxLines), closed ? lines[end]! : fence);
      out.push(CODE_CUT_NOTE);
    }
    i = end;
  }
  return { text: out.join("\n"), removed };
}

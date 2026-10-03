// Nudges the executor when it writes a tool call as text (Qwen's `<tool_call><function=…>` form)
// that the model server did not parse into a real tool call. Without this, pi takes the text as
// the final answer and the run ends with the work undone (4 of 62 executor runs in calibration).
// The turn gets a short notice and one more model request, at most LSO_NUDGE_MAX times per
// run; every nudge is logged to LSO_NUDGE_LOG. Loaded for every arm, so all arms are equal.
import { appendFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const TEXT_TOOL_CALL = /<tool_call>|<function=[\w.-]+>/;
const NOTICE =
	"Your last message contained a tool call written as text (for example `<tool_call>` or " +
	"`<function=...>`). It was not executed. Call tools through the tool interface, not in text.";

export default function (pi: ExtensionAPI) {
	const max = Number(process.env.LSO_NUDGE_MAX ?? "3");
	const log = process.env.LSO_NUDGE_LOG;
	let turn = 0;
	let nudges = 0;
	pi.on("turn_end", async (event, ctx) => {
		turn += 1;
		const message = event.message as { stopReason?: string; content?: unknown };
		if (ctx.signal?.aborted || message.stopReason === "aborted" || message.stopReason === "error") return;
		const content = Array.isArray(message.content) ? (message.content as { type?: string; text?: string }[]) : [];
		if (content.some((c) => c.type === "toolCall") || event.toolResults?.length) return;
		const text = content.map((c) => (c.type === "text" ? (c.text ?? "") : "")).join("\n");
		if (!TEXT_TOOL_CALL.test(text) || nudges >= max) return;
		nudges += 1;
		if (log) {
			const record = { turn, nudge: nudges, ts: Date.now() / 1000, excerpt: text.slice(0, 300) };
			appendFileSync(log, `${JSON.stringify(record)}\n`);
		}
		return {
			entries: [{ type: "custom_message", customType: "lso-nudge", content: NOTICE, display: true }],
			continue: true,
		};
	});
}

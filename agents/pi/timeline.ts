// Records when turns, model calls, and tool calls start and end (LSO_TIMELINE, JSONL), for the
// harness's MLflow trace: pi's JSON events carry no times for tool calls.
import { appendFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	const file = process.env.LSO_TIMELINE;
	if (!file) return;
	const mark = (record: Record<string, unknown>) =>
		appendFileSync(file, `${JSON.stringify({ t: Date.now(), ...record })}\n`);

	pi.on("turn_start", async () => mark({ event: "turn_start" }));
	pi.on("turn_end", async () => mark({ event: "turn_end" }));
	pi.on("message_start", async (event) => {
		if (event.message.role === "assistant") mark({ event: "model_start" });
	});
	pi.on("message_end", async (event) => {
		if (event.message.role === "assistant") mark({ event: "model_end" });
	});
	pi.on("tool_execution_start", async (event) =>
		mark({ event: "tool_start", id: event.toolCallId }),
	);
	pi.on("tool_execution_end", async (event) => mark({ event: "tool_end", id: event.toolCallId }));
}

// Stands in for the advisor plugin when a logged run is replayed (harness replay.py): answers
// consults, refuses calls and appends turn-end messages exactly as the log shows, so pi, fed the
// logged model replies by a mock server, rebuilds the run up to the fork consult. That consult
// gets the branch's answer (the logged advice, or a neutral reply); every later consult gets the
// neutral reply, and nothing more is refused or appended: after the fork the executor is on its
// own. The executor sees the same system prompt and consult tool as in the logged run.
//
// LSO_REPLAY: the script replay.py wrote (JSON). Not part of the pi bundle: the adapter writes
// this file into the container for replay arms only.
import { readFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface Script {
	fork: string;
	answers: Record<string, string>;
	refused: Record<string, string>;
	appended: Record<string, string[]>;
	later: string;
	guidance: string;
	tool: { description: string; parameters: unknown };
}

export default function (pi: ExtensionAPI) {
	const script = JSON.parse(readFileSync(process.env.LSO_REPLAY ?? "/run/lso/replay.json", "utf8")) as Script;
	let forked = false;
	let turns = 0;

	pi.on("before_agent_start", async (event) => {
		const options = event.systemPromptOptions;
		if (script.guidance && !options.appendSystemPrompt.includes(script.guidance)) {
			options.appendSystemPrompt = [options.appendSystemPrompt, script.guidance].filter(Boolean).join("\n\n");
		}
	});

	pi.on("tool_call", async (event) => {
		const reason = forked ? undefined : script.refused[event.toolCallId];
		if (reason) return { block: true, reason };
	});

	pi.on("turn_end", async () => {
		turns += 1;
		const entries = forked ? undefined : script.appended[String(turns)];
		if (!entries?.length) return;
		return {
			entries: entries.map((content) => ({ type: "custom_message", customType: "lso-advice", content, display: true })),
			continue: true,
		};
	});

	pi.registerTool({
		name: "consult",
		label: "Consult advisor",
		description: script.tool.description,
		parameters: script.tool.parameters as never,
		executionMode: "sequential",
		async execute(toolCallId: string) {
			const text = forked ? script.later : (script.answers[toolCallId] ?? script.later);
			if (toolCallId === script.fork) forked = true;
			return { content: [{ type: "text", text }], details: { replay: true } };
		},
	});
}

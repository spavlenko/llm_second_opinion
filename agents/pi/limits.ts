// Enforces the harness's turn limit (LSO_MAX_TURNS): after that many turns, record the reason
// in LSO_EXIT_FILE and abort, so the adapter can tell a turn limit from a finished run.
import { writeFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	const maxTurns = Number(process.env.LSO_MAX_TURNS ?? "0");
	const exitFile = process.env.LSO_EXIT_FILE;
	let turns = 0;
	let stopped = false;
	pi.on("turn_end", async (_event, ctx) => {
		// The turn that the abort cuts short also ends; it is not counted.
		if (stopped) return;
		turns += 1;
		if (maxTurns > 0 && turns >= maxTurns) {
			stopped = true;
			if (exitFile) writeFileSync(exitFile, JSON.stringify({ reason: "turn_limit", turns }));
			ctx.abort();
		}
	});
}

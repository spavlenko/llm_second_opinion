// `node dist/review-cli.js`: one review consult for the harness (`bench pick --review`). Reads a
// JSON request on stdin, writes the result as JSON on stdout. The endpoint is the metering
// proxy's route, so the request carries no secrets.
import { readFileSync } from "node:fs";
import { AdvisorClient } from "./client.js";
import type { ModelEndpoint } from "./contracts.js";
import { type ReviewRequest, review } from "./review.js";

interface Input {
  endpoint: ModelEndpoint;
  level: ReviewRequest["level"];
  task: string;
  issue: string;
  candidates: ReviewRequest["candidates"];
  templates: ReviewRequest["templates"];
  max_diff_tokens: number;
  max_answer_tokens: number | null;
  request_id?: string;
}

const input = JSON.parse(readFileSync(0, "utf8")) as Input;
const result = await review(
  {
    level: input.level,
    task: input.task,
    issue: input.issue,
    candidates: input.candidates,
    templates: input.templates,
    maxDiffTokens: input.max_diff_tokens,
    maxAnswerTokens: input.max_answer_tokens,
    requestId: input.request_id,
  },
  new AdvisorClient(input.endpoint, { env: {} }),
);
process.stdout.write(`${JSON.stringify(result)}\n`);

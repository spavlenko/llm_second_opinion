// Trigger engine, brief builder (L0–L3), consult budget, advisor client, and event emitter.
// Must not import pi: bindings for other agents reuse it.
export type * from "./contracts.js";
export { EventWriter, type EventInput } from "./events.js";
export { ConfigError, isAdvisorArm, loadRunConfig, validateRunConfig, type AdvisorRunConfig } from "./config.js";
export { placeholders, renderTemplate, type Vars } from "./template.js";
export {
  TEST_COMMAND,
  readsCode,
  filesRead,
  callSignature,
  editsFiles,
  errorLocations,
  errorSignature,
  errorText,
  failingTests,
  testRun,
  type TestRun,
  type ToolObservation,
} from "./observe.js";
export { MIN_HYPOTHESIS_WORDS, TriggerEngine, type Decision, type Fire, type Refusal, type Skip, type SkipReason } from "./triggers.js";
export { CODE_CUT_NOTE, limitCodeBlocks } from "./advice.js";
export { RoleMap, Redactor, stripCodeBlocks, stripInlineCode, type Role, type RoleMapOptions } from "./redact.js";
export {
  BRIEF_CUT_MARKER,
  DEFAULT_QUESTIONS,
  cutBrief,
  errorCategory,
  approxTokens,
  buildBrief,
  describeActions,
  type Brief,
  type BriefContext,
  type BriefRequest,
} from "./brief.js";
export {
  AdvisorClient,
  AdvisorClientError,
  type AdvisorClientLike,
  type AdvisorClientOptions,
  type ChatMessage,
  type Completion,
  type CompletionRequest,
} from "./client.js";
export {
  AdvisorSession,
  CONSULT_TOOL,
  CLARIFY_MAX_LINES,
  parseClarify,
  type ClarifyRequest,
  TRUNCATED_MARKER,
  type Advice,
  type AdviceRecord,
  type AdvisorSessionOptions,
  type ConsultArgs,
} from "./session.js";

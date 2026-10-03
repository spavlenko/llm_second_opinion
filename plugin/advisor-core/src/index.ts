// Trigger engine, brief builder (L0–L3), consult budget, advisor client, and event emitter.
// Must not import pi: bindings for other agents reuse it.
export type * from "./contracts.js";
export { EventWriter, type EventInput } from "./events.js";
export { ConfigError, isAdvisorArm, loadRunConfig, validateRunConfig, type AdvisorRunConfig } from "./config.js";
export { placeholders, renderTemplate, type Vars } from "./template.js";
export {
  TEST_COMMAND,
  readsCode,
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
export { MIN_HYPOTHESIS_WORDS, TriggerEngine, type Decision, type Fire, type Refusal } from "./triggers.js";
export { CODE_CUT_NOTE, limitCodeBlocks } from "./advice.js";
export { RoleMap, Redactor, stripCodeBlocks, stripInlineCode, type Role } from "./redact.js";
export {
  BRIEF_CUT_MARKER,
  DEFAULT_QUESTIONS,
  cutBrief,
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
  type Completion,
  type CompletionRequest,
} from "./client.js";
export {
  AdvisorSession,
  CONSULT_TOOL,
  TRUNCATED_MARKER,
  type Advice,
  type AdviceRecord,
  type AdvisorSessionOptions,
  type ConsultArgs,
} from "./session.js";

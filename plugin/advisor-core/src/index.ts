// Trigger engine, brief builder (L0–L3), consult budget, advisor client, and event emitter.
// Must not import pi: bindings for other agents reuse it.
export type * from "./contracts.js";
export { EventWriter, type EventInput } from "./events.js";
export { ConfigError, isAdvisorArm, loadRunConfig, validateRunConfig, type AdvisorRunConfig } from "./config.js";
export { placeholders, renderTemplate, type Vars } from "./template.js";
export {
  TEST_COMMAND,
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
export { TriggerEngine, type Decision, type Fire } from "./triggers.js";
export { RoleMap, Redactor, stripCodeBlocks, stripInlineCode, type Role } from "./redact.js";
export {
  DEFAULT_QUESTIONS,
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

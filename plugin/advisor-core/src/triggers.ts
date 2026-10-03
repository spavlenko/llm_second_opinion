// The trigger engine: decides when the harness consults the advisor, over the observation
// stream, and keeps the consult budget for harness triggers and the consult tool alike.
//
// Turns are counted from 0. Harness triggers are checked at the end of a turn (so advice can be
// injected before the next model call), at most one per turn, in the order before_done, orient,
// on_test_failure, stuck, periodic. `plan` fires once, before the first model call; `orient`
// also fires just before the first edit if it has not fired by then. A trigger whose condition
// is met but that cannot fire (tests passed, cooldown, reserve, budget) is recorded as a skip,
// and the next one in order may fire instead.
//
// The consult tool is subject to the consult rules (`AdvisorSettings.rules`): the executor must
// do its own work between consults. A refusal does not use up the budget. While before_done is
// enabled and has not fired, everything else may use at most `max_consults - reserve_for_end`.
import type { AdvisorSettings, Intervention } from "./contracts.js";
import {
  callSignature,
  editsFiles,
  errorSignature,
  filesRead,
  readsCode,
  testRun,
  type ToolObservation,
} from "./observe.js";

export interface Fire {
  intervention: Intervention;
  reason: string;
  turn: number;
}

/** `tests_passed`: the latest test run passed (stuck; before_done: since the last edit).
 * `cooldown`: within `cooldown_turns` of a consult. `reserved_for_end`: the consults left are
 * kept for before_done. `budget`: max_consults is spent. */
export type SkipReason = "tests_passed" | "cooldown" | "reserved_for_end" | "budget";

/** A harness trigger whose condition was met but which did not fire, and why. */
export interface Skip {
  intervention: Intervention;
  reason: SkipReason;
  turn: number;
}

/** What to do about a wanted consult: make it, or report that the budget is spent
 * (`report` is true the first time only, so budget_exhausted is emitted once). */
export type Decision = { kind: "fire"; fire: Fire } | { kind: "exhausted"; report: boolean; used: number; limit: number };

/** A consult rule turned the consult tool down: the rule's name and what the executor is told. */
export interface Refusal {
  kind: "refused";
  rule: "reserved_for_end" | "tool_cooldown_turns" | "min_own_actions" | "require_hypothesis";
  message: string;
}

/** `tried` or `hypothesis` shorter than this many words counts as missing. */
export const MIN_HYPOTHESIS_WORDS = 5;

const wordCount = (text: string | undefined) => (text ?? "").split(/\s+/).filter(Boolean).length;
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
// Shell commands that throw away the executor's own changes.
const BASH_REVERT = /\bgit\s+(checkout|restore|stash)\b/;

interface EditPair {
  path: string;
  oldText: string;
  newText: string;
}

/** The replacements an edit tool call made, in either of its argument shapes. */
function editPairs(obs: ToolObservation): EditPair[] {
  const path = typeof obs.args.path === "string" ? obs.args.path : "";
  const list = Array.isArray(obs.args.edits) ? (obs.args.edits as Record<string, unknown>[]) : [obs.args];
  return list
    .filter((e) => typeof e.oldText === "string" && typeof e.newText === "string")
    .map((e) => ({ path, oldText: e.oldText as string, newText: e.newText as string }));
}

export class TriggerEngine {
  turn = 0;
  consultsUsed = 0;
  private lastConsultTurn: number | null = null;
  private planDone = false;
  private orientDone = false;
  private beforeDoneFired = false;
  private exhaustedReported = false;
  /** Own tool calls since the last consult (or the start), for min_own_actions. */
  private ownActions = 0;
  /** Distinct files read, for orient. */
  private files = new Set<string>();
  private edited = false;
  private edits: EditPair[] = [];
  // stuck heuristic: reset by a consult and by progress (a test run with a new outcome, a new
  // distinct error from a build or run); a repeated error, or a revert of the executor's own
  // edit, counts toward it.
  private lastCall: string | null = null;
  private repeats = 0;
  private errors = new Map<string, number>();
  private quietTurns = 0;
  private editedThisTurn = false;
  private progressThisTurn = false;
  // test runs
  private failedTestRun: string | null = null;
  private lastOutcome: string | null = null;
  private latestTestPassed = false;
  private testSinceEditState: { failed: boolean } | null = null;
  private skips: Skip[] = [];

  constructor(private readonly settings: AdvisorSettings) {}

  private has(i: Intervention): boolean {
    return this.settings.interventions.includes(i);
  }

  get consultsLeft(): number {
    return Math.max(0, this.settings.max_consults - this.consultsUsed);
  }

  /** Has the executor changed a file yet? */
  get hasEdited(): boolean {
    return this.edited;
  }

  /** The latest run of the task's tests since the latest edit: null if none. */
  get testSinceEdit(): { failed: boolean } | null {
    return this.testSinceEditState;
  }

  /** Consults kept back from everything but before_done: `reserve_for_end` while before_done is
   * enabled and has not fired. */
  get reserved(): number {
    return this.has("before_done") && !this.beforeDoneFired ? this.settings.reserve_for_end : 0;
  }

  /** Triggers skipped since the last call, oldest first (for trigger_skipped events). */
  takeSkips(): Skip[] {
    const out = this.skips;
    this.skips = [];
    return out;
  }

  turnStart(turn: number): void {
    this.turn = turn;
    this.editedThisTurn = false;
    this.progressThisTurn = false;
    this.failedTestRun = null;
  }

  observe(obs: ToolObservation): void {
    const sig = callSignature(obs);
    this.repeats = sig === this.lastCall ? this.repeats + 1 : 1;
    this.lastCall = sig;
    this.ownActions += 1;
    for (const f of filesRead(obs)) this.files.add(f);
    if (editsFiles(obs)) {
      const revert = this.isRevert(obs);
      this.edited = true;
      this.testSinceEditState = null;
      if (!revert) this.editedThisTurn = true;
      if (obs.name === "edit") this.edits.push(...editPairs(obs));
    }
    const run = testRun(obs);
    const err = obs.isError || run?.failed ? errorSignature(obs.result) : "";
    if (run) {
      if (run.failed) {
        this.failedTestRun = run.buildFailed
          ? "the build failed"
          : run.failedTests.length
            ? `${run.failedTests.length} test(s) failed`
            : "the test run failed";
      }
      this.latestTestPassed = !run.failed;
      this.testSinceEditState = { failed: run.failed };
      const outcome = run.failed ? `failed ${run.buildFailed} ${[...run.failedTests].sort().join(" ")}` : "passed";
      // The same failure again (same failing tests, an error already seen) is not progress.
      const repeat = run.failed && outcome === this.lastOutcome && (!err || this.errors.has(err));
      this.lastOutcome = outcome;
      if (!repeat) this.progress();
    }
    if (err) {
      // A new distinct error from a build or a run is progress; from a read it is not.
      if (!this.errors.has(err) && (run || (obs.name === "bash" && !readsCode(obs)))) this.progress();
      this.errors.set(err, (this.errors.get(err) ?? 0) + 1);
    }
  }

  /** Does this file-changing call undo the executor's own change? `git checkout/restore/stash`
   * after an edit, or an edit whose every replacement is the inverse of an earlier one. */
  private isRevert(obs: ToolObservation): boolean {
    if (obs.name === "bash") return this.edited && BASH_REVERT.test(String(obs.args.command ?? ""));
    if (obs.name !== "edit") return false;
    const pairs = editPairs(obs);
    return (
      pairs.length > 0 &&
      pairs.every((p) => this.edits.some((q) => q.path === p.path && q.oldText === p.newText && q.newText === p.oldText))
    );
  }

  private progress(): void {
    this.repeats = 1;
    this.errors.clear();
    this.quietTurns = 0;
    this.progressThisTurn = true;
  }

  /** Before the first model call: the plan review. */
  start(): Decision | null {
    if (!this.has("plan") || this.planDone) return null;
    this.planDone = true;
    return this.attempt({ intervention: "plan", reason: "start of the run", turn: this.turn });
  }

  /** At the end of a turn: a harness trigger, if one is due. `stopping`: the turn made no tool
   * calls, so the executor is about to finish. */
  turnEnd(stopping = false): Decision | null {
    this.quietTurns = this.editedThisTurn || this.progressThisTurn ? 0 : this.quietTurns + 1;
    for (const fire of this.due(stopping)) {
      const decision = this.attempt(fire);
      if (decision) return decision;
    }
    return null;
  }

  /** The executor is about to make its first edit: `orient`, if it has not fired yet. This is
   * orient's last chance; if the cooldown, the reserve or the budget is in the way, it is
   * skipped. */
  beforeEdit(): Decision | null {
    if (!this.has("orient") || this.orientDone || this.edited) return null;
    this.orientDone = true;
    return this.attempt({ intervention: "orient", reason: "before the first edit", turn: this.turn });
  }

  private inCooldown(): boolean {
    return this.lastConsultTurn !== null && this.turn <= this.lastConsultTurn + this.settings.cooldown_turns;
  }

  /** Harness triggers whose condition is met this turn, in order. */
  private due(stopping: boolean): Fire[] {
    const turn = this.turn;
    const out: Fire[] = [];
    if (this.has("before_done") && !this.beforeDoneFired && stopping && this.edited) {
      out.push({ intervention: "before_done", reason: "stopped after editing", turn });
    }
    if (this.has("orient") && !this.orientDone) {
      if (this.edited) {
        this.orientDone = true; // too late: orientation is over once the executor has edited
      } else if (this.files.size >= this.settings.orient_after) {
        out.push({ intervention: "orient", reason: `${plural(this.files.size, "file")} read`, turn });
      }
    }
    if (this.has("on_test_failure") && this.failedTestRun) {
      out.push({ intervention: "on_test_failure", reason: this.failedTestRun, turn });
    }
    if (this.has("stuck")) {
      const reason = this.stuckReason();
      if (reason) out.push({ intervention: "stuck", reason, turn });
    }
    const every = this.settings.periodic_every;
    if (this.has("periodic") && every && (turn + 1) % every === 0) {
      out.push({ intervention: "periodic", reason: `every ${every} turns`, turn });
    }
    return out;
  }

  private stuckReason(): string | null {
    const t = this.settings.stuck;
    if (this.repeats >= t.repeat_calls) return `repeat_calls: the same call ${this.repeats} times in a row`;
    for (const [, n] of this.errors) if (n >= t.same_error) return `same_error: the same error ${n} times`;
    if (this.quietTurns >= t.no_diff_turns) return `no_diff_turns: ${this.quietTurns} turns without an edit or progress`;
    return null;
  }

  /** A harness trigger whose condition is met: fire it, or record why not. Null when it was
   * skipped for a reason other than the budget, so the caller may try the next trigger. */
  private attempt(fire: Fire): Decision | null {
    const reason = this.blocker(fire.intervention);
    if (!reason) return this.decide(fire);
    this.skips.push({ intervention: fire.intervention, reason, turn: fire.turn });
    // orient cannot come back once the reserve or the budget blocks it; stuck waits for its
    // condition to build up again rather than being skipped every turn.
    if (fire.intervention === "orient" && reason !== "cooldown") this.orientDone = true;
    if (fire.intervention === "stuck") this.resetStuck();
    return reason === "budget" ? this.decide(fire) : null; // the budget: exhausted, reported once
  }

  private blocker(i: Intervention): SkipReason | null {
    if (i === "stuck" && this.latestTestPassed) return "tests_passed";
    if (i === "before_done" && this.testSinceEditState?.failed === false) return "tests_passed";
    if (this.inCooldown()) return "cooldown";
    if (this.consultsUsed >= this.settings.max_consults) return "budget";
    if (i !== "before_done" && this.consultsUsed >= this.settings.max_consults - this.reserved) return "reserved_for_end";
    return null;
  }

  /** The executor asked (consult tool). The budget comes first (a request over it is
   * `exhausted`), then the reserve for before_done, then the consult rules; the harness
   * triggers' `cooldown_turns` does not apply. A refusal leaves the budget and the counters as
   * they were. */
  requestConsult(args: { tried?: string; hypothesis?: string } = {}): Decision | Refusal {
    if (this.consultsUsed < this.settings.max_consults) {
      const refusal = this.checkRules(args);
      if (refusal) return refusal;
    }
    return this.decide({ intervention: "consult", reason: "consult tool", turn: this.turn });
  }

  private checkRules(args: { tried?: string; hypothesis?: string }): Refusal | null {
    const rules = this.settings.rules;
    const last = this.lastConsultTurn;
    const max = this.settings.max_consults;
    if (this.consultsUsed >= max - this.reserved) {
      const kept = max - this.consultsUsed;
      return {
        kind: "refused",
        rule: "reserved_for_end",
        message:
          `${kept === 1 ? "The last advisor consult is" : `The last ${kept} advisor consults are`} kept for a final ` +
          `check before you finish (${this.consultsUsed} of ${max} used). Continue on your own.`,
      };
    }
    if (last !== null && rules.tool_cooldown_turns > 0 && this.turn <= last + rules.tool_cooldown_turns) {
      const left = last + rules.tool_cooldown_turns - this.turn + 1;
      return {
        kind: "refused",
        rule: "tool_cooldown_turns",
        message: `Too soon after the last consult: keep working on your own for ${plural(left, "more turn")}, then consult again if you still need to.`,
      };
    }
    if (this.ownActions < rules.min_own_actions) {
      return {
        kind: "refused",
        rule: "min_own_actions",
        message:
          `Investigate first: run or read something ${last === null ? "yourself before consulting" : "since your last consult"} ` +
          `(${this.ownActions} of ${plural(rules.min_own_actions, "tool call")} so far).`,
      };
    }
    if (
      rules.require_hypothesis &&
      (wordCount(args.tried) < MIN_HYPOTHESIS_WORDS || wordCount(args.hypothesis) < MIN_HYPOTHESIS_WORDS)
    ) {
      return {
        kind: "refused",
        rule: "require_hypothesis",
        message: `Say what you tried and what you think the cause is: \`tried\` and \`hypothesis\` need at least ${MIN_HYPOTHESIS_WORDS} words each.`,
      };
    }
    return null;
  }

  private decide(fire: Fire): Decision {
    if (this.consultsUsed >= this.settings.max_consults) {
      const report = !this.exhaustedReported;
      this.exhaustedReported = true;
      return { kind: "exhausted", report, used: this.consultsUsed, limit: this.settings.max_consults };
    }
    this.consultsUsed += 1;
    this.lastConsultTurn = this.turn;
    if (fire.intervention === "before_done") this.beforeDoneFired = true;
    if (fire.intervention === "orient") this.orientDone = true;
    this.ownActions = 0;
    this.resetStuck();
    return { kind: "fire", fire };
  }

  private resetStuck(): void {
    this.repeats = 0;
    this.lastCall = null;
    this.errors.clear();
    this.quietTurns = 0;
  }
}

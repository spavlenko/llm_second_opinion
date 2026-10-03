// The trigger engine: decides when the harness consults the advisor, over the observation
// stream, and keeps the consult budget for harness triggers and the consult tool alike.
//
// Turns are counted from 0. Harness triggers are checked at the end of a turn (so advice can be
// injected before the next model call), at most one per turn, in the order before_done, orient,
// on_test_failure, stuck, periodic. `plan` fires once, before the first model call; `orient`
// also fires just before the first edit if it has not fired by then.
//
// The consult tool is subject to the consult rules (`AdvisorSettings.rules`): the executor must
// do its own work between consults. A refusal does not use up the budget.
import type { AdvisorSettings, Intervention } from "./contracts.js";
import { callSignature, editsFiles, errorSignature, readsCode, testRun, type ToolObservation } from "./observe.js";

export interface Fire {
  intervention: Intervention;
  reason: string;
  turn: number;
}

/** What to do about a wanted consult: make it, or report that the budget is spent
 * (`report` is true the first time only, so budget_exhausted is emitted once). */
export type Decision = { kind: "fire"; fire: Fire } | { kind: "exhausted"; report: boolean; used: number; limit: number };

/** A consult rule turned the consult tool down: the rule's name and what the executor is told. */
export interface Refusal {
  kind: "refused";
  rule: "tool_cooldown_turns" | "min_own_actions" | "require_hypothesis";
  message: string;
}

/** `tried` or `hypothesis` shorter than this many words counts as missing. */
export const MIN_HYPOTHESIS_WORDS = 5;

const wordCount = (text: string | undefined) => (text ?? "").split(/\s+/).filter(Boolean).length;
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export class TriggerEngine {
  turn = 0;
  consultsUsed = 0;
  private lastConsultTurn: number | null = null;
  private planDone = false;
  private orientDone = false;
  private beforeDoneDone = false;
  private exhaustedReported = false;
  /** Own tool calls since the last consult (or the start), for min_own_actions. */
  private ownActions = 0;
  private readActions = 0;
  private edited = false;
  // stuck heuristic
  private lastCall: string | null = null;
  private repeats = 0;
  private errors = new Map<string, number>();
  private quietTurns = 0;
  private editedThisTurn = false;
  // on_test_failure
  private failedTestRun: string | null = null;

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

  turnStart(turn: number): void {
    this.turn = turn;
    this.editedThisTurn = false;
    this.failedTestRun = null;
  }

  observe(obs: ToolObservation): void {
    const sig = callSignature(obs);
    this.repeats = sig === this.lastCall ? this.repeats + 1 : 1;
    this.lastCall = sig;
    this.ownActions += 1;
    if (readsCode(obs)) this.readActions += 1;
    if (editsFiles(obs)) {
      this.editedThisTurn = true;
      this.edited = true;
    }
    const run = testRun(obs);
    if (run?.failed) {
      this.failedTestRun = run.buildFailed
        ? "the build failed"
        : run.failedTests.length
          ? `${run.failedTests.length} test(s) failed`
          : "the test run failed";
    }
    if (obs.isError || run?.failed) {
      const err = errorSignature(obs.result);
      if (err) this.errors.set(err, (this.errors.get(err) ?? 0) + 1);
    }
  }

  /** Before the first model call: the plan review. */
  start(): Decision | null {
    if (!this.has("plan") || this.planDone) return null;
    this.planDone = true;
    return this.decide({ intervention: "plan", reason: "start of the run", turn: this.turn });
  }

  /** At the end of a turn: a harness trigger, if one is due. `stopping`: the turn made no tool
   * calls, so the executor is about to finish. */
  turnEnd(stopping = false): Decision | null {
    this.quietTurns = this.editedThisTurn ? 0 : this.quietTurns + 1;
    if (this.inCooldown()) return null;
    const fire = this.due(stopping);
    return fire ? this.decide(fire) : null;
  }

  /** The executor is about to make its first edit: `orient`, if it has not fired yet. This is
   * orient's last chance; if the cooldown or the budget is in the way, it is skipped. */
  beforeEdit(): Decision | null {
    if (!this.has("orient") || this.orientDone || this.edited) return null;
    this.orientDone = true;
    if (this.inCooldown()) return null;
    return this.decide({ intervention: "orient", reason: "before the first edit", turn: this.turn });
  }

  private inCooldown(): boolean {
    return this.lastConsultTurn !== null && this.turn <= this.lastConsultTurn + this.settings.cooldown_turns;
  }

  private due(stopping: boolean): Fire | null {
    const turn = this.turn;
    if (this.has("before_done") && !this.beforeDoneDone && stopping && this.edited) {
      this.beforeDoneDone = true;
      return { intervention: "before_done", reason: "stopped after editing", turn };
    }
    if (this.has("orient") && !this.orientDone) {
      if (this.edited) {
        this.orientDone = true; // too late: orientation is over once the executor has edited
      } else if (this.readActions >= this.settings.orient_after) {
        this.orientDone = true;
        return { intervention: "orient", reason: plural(this.readActions, "read action"), turn };
      }
    }
    if (this.has("on_test_failure") && this.failedTestRun) {
      return { intervention: "on_test_failure", reason: this.failedTestRun, turn };
    }
    if (this.has("stuck")) {
      const t = this.settings.stuck;
      if (this.repeats >= t.repeat_calls) {
        return { intervention: "stuck", reason: `repeat_calls: the same call ${this.repeats} times in a row`, turn };
      }
      for (const [, n] of this.errors) {
        if (n >= t.same_error) return { intervention: "stuck", reason: `same_error: the same error ${n} times`, turn };
      }
      if (this.quietTurns >= t.no_diff_turns) {
        return { intervention: "stuck", reason: `no_diff_turns: ${this.quietTurns} turns without a file edit`, turn };
      }
    }
    const every = this.settings.periodic_every;
    if (this.has("periodic") && every && (turn + 1) % every === 0) {
      return { intervention: "periodic", reason: `every ${every} turns`, turn };
    }
    return null;
  }

  /** The executor asked (consult tool). The budget comes first (a request over it is
   * `exhausted`), then the consult rules; the harness triggers' `cooldown_turns` does not
   * apply. A refusal leaves the budget and the counters as they were. */
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

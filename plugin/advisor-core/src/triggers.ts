// The trigger engine: decides when the harness consults the advisor, over the observation
// stream, and keeps the consult budget for harness triggers and the consult tool alike.
//
// Turns are counted from 0. Harness triggers are checked at the end of a turn (so advice can be
// injected before the next model call), at most one per turn, in the order on_test_failure,
// stuck, periodic. `plan` fires once, before the first model call.
import type { AdvisorSettings, Intervention } from "./contracts.js";
import { callSignature, editsFiles, errorSignature, testRun, type ToolObservation } from "./observe.js";

export interface Fire {
  intervention: Intervention;
  reason: string;
  turn: number;
}

/** What to do about a wanted consult: make it, or report that the budget is spent
 * (`report` is true the first time only, so budget_exhausted is emitted once). */
export type Decision = { kind: "fire"; fire: Fire } | { kind: "exhausted"; report: boolean; used: number; limit: number };

export class TriggerEngine {
  turn = 0;
  consultsUsed = 0;
  private lastConsultTurn: number | null = null;
  private planDone = false;
  private exhaustedReported = false;
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

  turnStart(turn: number): void {
    this.turn = turn;
    this.editedThisTurn = false;
    this.failedTestRun = null;
  }

  observe(obs: ToolObservation): void {
    const sig = callSignature(obs);
    this.repeats = sig === this.lastCall ? this.repeats + 1 : 1;
    this.lastCall = sig;
    if (editsFiles(obs)) this.editedThisTurn = true;
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

  /** At the end of a turn: on_test_failure, stuck, or periodic, if one is due. */
  turnEnd(): Decision | null {
    this.quietTurns = this.editedThisTurn ? 0 : this.quietTurns + 1;
    if (this.lastConsultTurn !== null && this.turn <= this.lastConsultTurn + this.settings.cooldown_turns) return null;
    const fire = this.due();
    return fire ? this.decide(fire) : null;
  }

  private due(): Fire | null {
    const turn = this.turn;
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

  /** The executor asked (consult tool): only the budget applies, not the cooldown. */
  requestConsult(): Decision {
    return this.decide({ intervention: "consult", reason: "consult tool", turn: this.turn });
  }

  private decide(fire: Fire): Decision {
    if (this.consultsUsed >= this.settings.max_consults) {
      const report = !this.exhaustedReported;
      this.exhaustedReported = true;
      return { kind: "exhausted", report, used: this.consultsUsed, limit: this.settings.max_consults };
    }
    this.consultsUsed += 1;
    this.lastConsultTurn = this.turn;
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

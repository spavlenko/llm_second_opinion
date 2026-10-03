import { describe, expect, it } from "vitest";
import type { ToolObservation } from "../src/observe.js";
import { TriggerEngine } from "../src/triggers.js";
import { settings } from "./helpers.js";

const call = (command: string, result = "ok", isError = false): ToolObservation => ({
  name: "bash",
  args: { command },
  result,
  isError,
});
const edit: ToolObservation = { name: "edit", args: { path: "a.cpp", edits: [] }, result: "ok", isError: false };

/** Runs turns of tool calls; returns the decision at each turn end. */
function run(engine: TriggerEngine, turns: ToolObservation[][], from = 0) {
  return turns.map((calls, i) => {
    engine.turnStart(from + i);
    for (const c of calls) engine.observe(c);
    return engine.turnEnd();
  });
}

const fired = (d: ReturnType<TriggerEngine["turnEnd"]>) => (d?.kind === "fire" ? d.fire.intervention : (d?.kind ?? null));

describe("trigger engine", () => {
  it("plan fires once, at the start", () => {
    const engine = new TriggerEngine(settings({ interventions: ["plan"] }));
    expect(engine.start()).toEqual({ kind: "fire", fire: { intervention: "plan", reason: "start of the run", turn: 0 } });
    expect(engine.start()).toBeNull();
    expect(new TriggerEngine(settings({ interventions: ["consult"] })).start()).toBeNull();
  });

  it("stuck: the same call repeat_calls times in a row", () => {
    const engine = new TriggerEngine(settings({ interventions: ["stuck"] }));
    const d = run(engine, [[call("ls"), edit], [call("ls"), call("ls")], [call("ls")]]);
    expect(d.map(fired)).toEqual([null, null, "stuck"]);
    expect(d[2]).toMatchObject({ fire: { reason: expect.stringMatching(/^repeat_calls/), turn: 2 } });
  });

  it("stuck: the same error same_error times, not necessarily in a row", () => {
    const stuck = { repeat_calls: 9, same_error: 2, no_diff_turns: 9 };
    const engine = new TriggerEngine(settings({ interventions: ["stuck"], stuck }));
    const err = (n: number) => call(`g++ ${n}`, `a.cpp:${n}: error: boom`, true);
    expect(run(engine, [[err(1), edit], [call("ls"), edit], [err(2), edit]]).map(fired)).toEqual([null, null, "stuck"]);
  });

  it("stuck: no_diff_turns turns without a file edit", () => {
    const stuck = { repeat_calls: 9, same_error: 9, no_diff_turns: 2 };
    const engine = new TriggerEngine(settings({ interventions: ["stuck"], stuck }));
    expect(run(engine, [[call("a")], [edit], [call("b")], [call("c")]]).map(fired)).toEqual([null, null, null, "stuck"]);
  });

  it("on_test_failure: a failing run of the task's tests, even piped through tail", () => {
    const engine = new TriggerEngine(settings({ interventions: ["on_test_failure"] }));
    const pass = call("/opt/lso/run-tests", "100% tests passed, 0 tests failed out of 4");
    const fail = call("/opt/lso/run-tests | tail", "75% tests passed, 1 tests failed out of 4");
    const d = run(engine, [[pass], [call("make", "x", true)], [fail]]);
    expect(d.map(fired)).toEqual([null, null, "on_test_failure"]);
    expect(d[2]).toMatchObject({ fire: { reason: "the test run failed" } });
  });

  it("periodic: every periodic_every turns", () => {
    const engine = new TriggerEngine(settings({ interventions: ["periodic"], periodic_every: 2 }));
    expect(run(engine, [[edit], [edit], [edit], [edit]]).map(fired)).toEqual([null, "periodic", null, "periodic"]);
  });

  it("one trigger per turn, on_test_failure first", () => {
    const engine = new TriggerEngine(
      settings({ interventions: ["stuck", "on_test_failure", "periodic"], periodic_every: 1 }),
    );
    const fail = call("/opt/lso/run-tests", "x", true);
    expect(run(engine, [[fail, fail, fail]]).map(fired)).toEqual(["on_test_failure"]);
  });

  it("cooldown: no harness trigger for cooldown_turns turns after any consult", () => {
    const engine = new TriggerEngine(
      settings({ interventions: ["periodic", "consult"], periodic_every: 1, cooldown_turns: 2 }),
    );
    engine.turnStart(0);
    expect(engine.requestConsult().kind).toBe("fire");
    expect(engine.turnEnd()).toBeNull(); // not in the consult's own turn
    expect(run(engine, [[edit], [edit], [edit]], 1).map(fired)).toEqual([null, null, "periodic"]);
  });

  it("the consult tool is not subject to the cooldown", () => {
    const engine = new TriggerEngine(settings({ cooldown_turns: 5 }));
    engine.turnStart(0);
    expect(engine.requestConsult().kind).toBe("fire");
    expect(engine.requestConsult().kind).toBe("fire");
  });

  it("a consult resets the stuck counters", () => {
    const engine = new TriggerEngine(settings({ interventions: ["stuck", "consult"] }));
    run(engine, [[call("ls"), call("ls")]]);
    engine.turnStart(1);
    engine.requestConsult();
    engine.observe(call("ls"));
    expect(engine.turnEnd()).toBeNull();
  });

  it("max_consults: budget_exhausted is reported once", () => {
    const engine = new TriggerEngine(settings({ max_consults: 1 }));
    engine.turnStart(0);
    expect(engine.requestConsult().kind).toBe("fire");
    expect(engine.consultsLeft).toBe(0);
    expect(engine.requestConsult()).toEqual({ kind: "exhausted", report: true, used: 1, limit: 1 });
    expect(engine.requestConsult()).toEqual({ kind: "exhausted", report: false, used: 1, limit: 1 });
  });

  it("orient: after orient_after distinct files read, before on_test_failure in the same turn, once", () => {
    const engine = new TriggerEngine(settings({ interventions: ["orient", "on_test_failure"], orient_after: 2 }));
    const read: ToolObservation = { name: "read", args: { path: "a.cpp" }, result: "x", isError: false };
    const fail = call("/opt/lso/run-tests", "x", true);
    const d = run(engine, [[read, call("make", "x"), call("cat a.cpp")], [call("grep -n foo b.cpp"), fail], [fail]]);
    expect(d.map(fired)).toEqual([null, "orient", "on_test_failure"]);
    expect(d[1]).toMatchObject({ fire: { reason: "2 files read", turn: 1 } });
    expect(engine.beforeEdit()).toBeNull();
  });

  it("orient before the first edit respects the cooldown; it is skipped, not postponed", () => {
    const engine = new TriggerEngine(settings({ interventions: ["orient", "consult"], cooldown_turns: 1 }));
    engine.turnStart(0);
    expect(engine.requestConsult().kind).toBe("fire");
    engine.turnStart(1);
    expect(engine.beforeEdit()).toBeNull();
    engine.observe(edit);
    expect(run(engine, [[call("cat a"), call("cat b"), call("cat c")]], 2).map(fired)).toEqual([null]);
  });

  it("before_done: once, on a turn without tool calls after an edit, within cooldown and budget", () => {
    const engine = new TriggerEngine(settings({ interventions: ["before_done", "periodic"], periodic_every: 1, max_consults: 3 }));
    engine.turnStart(0);
    expect(fired(engine.turnEnd(true))).toBe("periodic"); // no edit yet
    engine.turnStart(1);
    engine.observe(edit);
    expect(fired(engine.turnEnd(false))).toBe("periodic"); // not stopping
    engine.turnStart(2);
    expect(engine.turnEnd(true)).toMatchObject({ kind: "fire", fire: { intervention: "before_done", reason: "stopped after editing", turn: 2 } });
    engine.turnStart(3);
    expect(engine.turnEnd(true)).toMatchObject({ kind: "exhausted", report: true }); // periodic; before_done was once

    const cooled = new TriggerEngine(settings({ interventions: ["before_done", "consult"], cooldown_turns: 1 }));
    cooled.turnStart(0);
    cooled.requestConsult();
    cooled.turnStart(1);
    cooled.observe(edit);
    expect(cooled.turnEnd(true)).toBeNull(); // the cooldown
    cooled.turnStart(2);
    expect(fired(cooled.turnEnd(true))).toBe("before_done");
  });

  it("consult rules apply to the consult tool only, in order: budget, cooldown, own actions, hypothesis", () => {
    const rules = { min_own_actions: 2, tool_cooldown_turns: 1, require_hypothesis: true, max_advice_code_lines: null };
    const engine = new TriggerEngine(settings({ interventions: ["consult", "periodic"], periodic_every: 1, rules }));
    const words = { tried: "ran the tests and read code", hypothesis: "the loop stops one item early" };
    engine.turnStart(0);
    expect(engine.requestConsult(words)).toMatchObject({ kind: "refused", rule: "min_own_actions" });
    engine.observe(edit);
    engine.observe(edit);
    expect(engine.requestConsult({ tried: "x" })).toMatchObject({ kind: "refused", rule: "require_hypothesis" });
    expect(engine.requestConsult(words).kind).toBe("fire");
    engine.turnStart(1);
    engine.observe(edit);
    engine.observe(edit);
    expect(engine.requestConsult(words)).toMatchObject({ kind: "refused", rule: "tool_cooldown_turns" });
    expect(engine.consultsUsed).toBe(1);
    engine.turnStart(2);
    expect(fired(engine.turnEnd())).toBe("periodic"); // a harness trigger is not refused by the rules
  });

  it("max_consults 0: even the plan review is refused", () => {
    expect(new TriggerEngine(settings({ max_consults: 0 })).start()).toMatchObject({ kind: "exhausted", report: true });
  });
});

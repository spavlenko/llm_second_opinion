import { describe, expect, it } from "vitest";
import {
  callSignature,
  editsFiles,
  errorLocations,
  errorSignature,
  errorText,
  failingTests,
  testRun,
} from "../src/observe.js";

const CTEST_FAIL = `Test project /build
1/3 Test #1: test-unicode1 ....................   Passed    0.02 sec
2/3 Test #2: test-conversions .................***Failed    0.31 sec
3/3 Test #3: test-regression ..................***Exception: SegFault  0.01 sec

33% tests passed, 2 tests failed out of 3

The following tests FAILED:
\t  2 - test-conversions (Failed)
\t  3 - test-regression (SEGFAULT)
Errors while running CTest
Command exited with code 8`;

const bash = (command: string, result: string, isError = false) => ({ name: "bash", args: { command }, result, isError });

describe("observations", () => {
  it("recognises a failing test run and its failing tests", () => {
    const run = testRun(bash("/opt/lso/run-tests 2>&1 | tail -40", CTEST_FAIL));
    expect(run).toEqual({ failed: true, failedTests: ["test-conversions", "test-regression"], buildFailed: false });
  });

  it("a passing run and a build failure", () => {
    expect(testRun(bash("/opt/lso/run-tests", "100% tests passed, 0 tests failed out of 3"))?.failed).toBe(false);
    const out = "run-tests: build failed; last lines of /tmp/build.log:\nfoo.cpp:3:1: error: x";
    expect(testRun(bash("/opt/lso/run-tests", out))).toMatchObject({ failed: true, buildFailed: true });
  });

  it("other commands are not test runs", () => {
    expect(testRun(bash("make test", CTEST_FAIL))).toBeNull();
    expect(testRun({ name: "read", args: { path: "/opt/lso/run-tests" }, result: "", isError: false })).toBeNull();
  });

  it("failing tests from the per-test lines alone", () => {
    expect(failingTests("Test #7: test-foo ......***Failed  1.0 sec")).toEqual(["test-foo"]);
  });

  it("error text: error lines, else the last lines; never the exit status", () => {
    const out = "building\nsrc/a.cpp:10:5: error: no member named 'x'\nnote: here\nCommand exited with code 2";
    expect(errorText(out)).toBe("src/a.cpp:10:5: error: no member named 'x'");
    expect(errorText("one\ntwo\nthree\nfour\nCommand exited with code 1")).toBe("two\nthree\nfour");
  });

  it("error signatures ignore numbers and whitespace", () => {
    expect(errorSignature("a.cpp:10:5: error: bad  thing")).toBe(errorSignature("a.cpp:12:7: error: bad thing"));
    expect(errorSignature("a.cpp:10: error: bad")).not.toBe(errorSignature("a.cpp:10: error: worse"));
  });

  it("call signatures do not depend on argument order", () => {
    const a = { name: "read", args: { path: "x", limit: 5 }, result: "", isError: false };
    const b = { name: "read", args: { limit: 5, path: "x" }, result: "other", isError: false };
    expect(callSignature(a)).toBe(callSignature(b));
  });

  it("file edits: edit and write tools, in-place shell edits, not failed calls", () => {
    expect(editsFiles({ name: "edit", args: {}, result: "", isError: false })).toBe(true);
    expect(editsFiles({ name: "edit", args: {}, result: "", isError: true })).toBe(false);
    expect(editsFiles(bash("sed -i 's/a/b/' x.cpp", ""))).toBe(true);
    expect(editsFiles(bash("cat x.cpp > /dev/null 2>&1", ""))).toBe(false);
  });

  it("locations in compiler and test output", () => {
    const out = "include/x/y.hpp:120:9: error: e\n/testbed/tests/t.cpp(44): FAILED\ninclude/x/y.hpp:120:9: note";
    expect(errorLocations(out)).toEqual([
      { path: "include/x/y.hpp", line: 120 },
      { path: "/testbed/tests/t.cpp", line: 44 },
    ]);
  });
});

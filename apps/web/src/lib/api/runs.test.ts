import { describe, expect, it } from "vitest";

import { mockRunSteps } from "@/mocks/runs";

import { aggregate, bar, filesOf, filesRead, lanes, spanOf, stepTitle, type Step } from "./runs";

function step(idx: number, over: Partial<Step>): Step {
  return {
    idx,
    kind: "tool",
    name: "code_search",
    input: {},
    summary: "",
    tokens: 0,
    ms: 100,
    offset_ms: 0,
    has_output: true,
    ...over,
  };
}

describe("a run's steps on the timeline", () => {
  it("says what each step did", () => {
    expect(stepTitle(step(0, { kind: "llm", name: "claude", input: { turn: 2 } }))).toBe(
      "Model turn 2",
    );
    expect(stepTitle(step(1, { name: "read_code", input: { path: "a/b.py" } }))).toBe(
      "Reading files · a/b.py",
    );
    expect(stepTitle(step(2, { kind: "retrieval", name: "round0" }))).toBe(
      "Searching before the first turn",
    );
    expect(stepTitle(step(3, { kind: "verify", name: "verify" }))).toBe(
      "Checking the answer against the code",
    );
  });

  it("folds repeated calls of one tool, and only those", () => {
    const steps = [
      step(0, { kind: "llm", name: "claude", input: { turn: 1 } }),
      step(1, { name: "code_search" }),
      step(2, { name: "code_search" }),
      step(3, { name: "code_search" }),
      step(4, { name: "read_code", input: { path: "x.py" } }),
      step(5, { name: "code_search" }),
    ];
    expect(aggregate(steps).map((g) => [g.label, g.steps.map((s) => s.idx)])).toEqual([
      ["Model turn 1", [0]],
      ["Searching code ×3", [1, 2, 3]],
      ["Reading files · x.py", [4]],
      ["Searching code", [5]],
    ]);
  });

  it("puts calls made at once on rows of their own, and later ones on the first free row", () => {
    const steps = [
      step(0, { offset_ms: 0, ms: 100 }),
      step(1, { offset_ms: 100, ms: 300 }),
      step(2, { offset_ms: 100, ms: 200 }),
      step(3, { offset_ms: 300, ms: 50 }),
      step(4, { offset_ms: 400, ms: 10 }),
    ];
    expect([...lanes(steps)].sort((a, b) => a[0] - b[0])).toEqual([
      [0, 0],
      [1, 0],
      [2, 1],
      [3, 1],
      [4, 0],
    ]);
    expect(spanOf(steps, null)).toBe(410);
    expect(bar(step(1, { offset_ms: 100, ms: 300 }), 400)).toEqual({ left: 0.25, width: 0.75 });
  });

  it("finds the files a step read, and the step that first read each", () => {
    expect(filesOf(step(0, { name: "read_code", input: { path: "a.py" } }))).toEqual(["a.py"]);
    expect(filesOf(step(0, { name: "list_dir", input: { path: "backend" } }))).toEqual([]);
    const submitted = step(0, {
      name: "submit_evidence",
      input: { candidates: [{ target: "b.py" }, { target: "sym:b.py::f" }, { target: "b.py" }] },
    });
    expect(filesOf(submitted)).toEqual(["b.py"]);
    const recorded = mockRunSteps();
    expect(recorded).toHaveLength(40);
    const read = filesRead(recorded);
    const first = recorded.find((s) => s.name === "read_code");
    expect(read[0]).toEqual({ path: first?.input.path, idx: first?.idx });
    expect(new Set(read.map((f) => f.path)).size).toBe(read.length);
  });
});

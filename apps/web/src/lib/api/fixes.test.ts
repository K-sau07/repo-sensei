import { describe, expect, it } from "vitest";

import {
  frameHref,
  frameLabel,
  isOpen,
  looksLikeIssue,
  STAGES,
  STAGE_LABELS,
  statusTone,
  type Frame,
} from "./fixes";

const frame = (fields: Partial<Frame>): Frame => ({
  file: "/srv/app/app/crud.py",
  line: 61,
  function: "count_items",
  path: "backend/app/crud.py",
  in_repo: true,
  library: false,
  ...fields,
});

describe("a fix's frames", () => {
  it("open the repository's file at their line, and a library's nowhere", () => {
    expect(frameHref("acme", "api", frame({}))).toBe("/acme/api/code/backend/app/crud.py?L=61");
    expect(frameHref("acme", "api", frame({ in_repo: false, path: null }))).toBeNull();
    expect(frameLabel(frame({}))).toBe("backend/app/crud.py:61");
    expect(frameLabel(frame({ path: null, file: "node:internal/x.js", line: 3 }))).toBe(
      "node:internal/x.js:3",
    );
  });
});

describe("a fix's state", () => {
  it("reads at a glance: waiting on people, going, done or stopped", () => {
    expect(statusTone("awaiting_approval")).toBe("warn");
    expect(statusTone("running")).toBe("info");
    expect(statusTone("succeeded")).toBe("ok");
    expect(statusTone("blocked")).toBe("danger");
    expect(statusTone("not_reproduced")).toBe("neutral");
    expect(isOpen("needs_triage")).toBe(true);
    expect(isOpen("rejected")).toBe(false);
    expect(STAGES.map((stage) => STAGE_LABELS[stage])).toEqual([
      "Intake",
      "Investigate",
      "Reproduce",
      "Root cause",
      "Plan",
      "Approve",
      "Patch",
      "Verify",
      "PR",
      "Done",
    ]);
  });

  it("starts from an issue given as a number, #number or its URL", () => {
    expect(looksLikeIssue("42")).toBe(true);
    expect(looksLikeIssue(" #42 ")).toBe(true);
    expect(looksLikeIssue("https://github.com/acme/api/issues/42")).toBe(true);
    expect(looksLikeIssue("https://github.com/acme/api/pull/42")).toBe(false);
    expect(looksLikeIssue("the login bug")).toBe(false);
  });
});

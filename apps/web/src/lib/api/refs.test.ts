import { describe, expect, it } from "vitest";

import { concerns } from "@/components/repo/new-commits-banner";
import { pinnedBehind } from "@/components/repo/pinned-stale";
import { withRef } from "@/components/repo/use-ref";
import { describeStatus, updatePercent } from "@/components/repos/repo-status";
import { updateLabel, useRepoUpdates, type RepoUpdate } from "@/components/repos/repo-updates";
import type { ActiveJob } from "@/lib/api/types";

import {
  citedKey,
  citedKeys,
  readable,
  reasons,
  refLabel,
  refParam,
  refStatusLabel,
  staleFiles,
  staleLabel,
  type RepoRef,
} from "./refs";

const SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e";
const NEW = "c0ffee1e5d0b6a0c8c1b7d3f2e9a4b5c6d7e8f90";

function ref(over: Partial<RepoRef>): RepoRef {
  return {
    name: "main",
    kind: "branch",
    default: true,
    status: "indexed",
    sha: SHA,
    indexed_at: null,
    overlay_of: null,
    ...over,
  };
}

describe("the ref switcher", () => {
  it("names what the page is read at", () => {
    expect(refLabel(null, "main")).toBe("main");
    expect(refLabel("release", "main")).toBe("release");
    expect(refLabel("pull/12", "main")).toBe("#12");
    expect(refLabel(SHA, "main")).toBe("b1125d8");
    expect(refLabel("b1125d8", "main")).toBe("b1125d8"); // a short id could be a branch: as typed
  });

  it("says what each ref's index is, and only an indexed one can be picked", () => {
    expect(refStatusLabel(ref({}))).toBe("Indexed at b1125d8");
    expect(refStatusLabel(ref({ status: "indexing" }))).toBe("Updating from b1125d8");
    expect(refStatusLabel(ref({ status: "indexing", sha: null }))).toBe("Indexing…");
    expect(refStatusLabel(ref({ status: "failed" }))).toBe("Last run failed · b1125d8 still shown");
    expect(refStatusLabel(ref({ status: "not_indexed", sha: null }))).toBe("Not indexed");
    expect(readable(ref({ status: "failed" }))).toBe(true); // its last index is still read
    expect(readable(ref({ status: "indexing", sha: null }))).toBe(false);
  });

  it("leaves the default branch out of the URL, and the tabs keep the ref", () => {
    expect(refParam(ref({}))).toBeNull();
    expect(refParam(ref({ name: "pull/7", kind: "pull_request", default: false }))).toBe("pull/7");
    expect(withRef("/acme/api/ask", null)).toBe("/acme/api/ask");
    expect(withRef("/acme/api/ask", "release")).toBe("/acme/api/ask?ref=release");
    expect(withRef("/acme/api/trace?from=x", "pull/7")).toBe("/acme/api/trace?from=x&ref=pull%2F7");
  });
});

describe("stale citations", () => {
  const cited = [
    { sha: SHA.toUpperCase(), path: "backend/app/models.py" },
    { sha: SHA, path: "backend/app/api/routes/items.py" },
    { sha: SHA, path: "backend/app/models.py" },
  ];

  it("checks each distinct citation once, in a stable order", () => {
    expect(citedKeys(cited)).toEqual([
      `${SHA}:backend/app/api/routes/items.py`,
      `${SHA}:backend/app/models.py`,
    ]);
    expect(citedKeys([])).toEqual([]);
  });

  it("counts the files that changed or went since, each once", () => {
    const checked = reasons({
      sha: NEW,
      citations: [
        { sha: SHA, path: "backend/app/models.py", stale: true, reason: "changed" },
        { sha: SHA, path: "backend/app/api/routes/items.py", stale: false, reason: "unchanged" },
      ],
    });
    expect(checked.get(citedKey(SHA, "backend/app/models.py"))).toBe("changed");
    expect(staleFiles(cited, checked)).toEqual(["backend/app/models.py"]);
    expect(staleLabel(["a"])).toBe("1 cited file changed since");
    expect(staleLabel(["a", "b"])).toBe("2 cited files changed since");
    // A cited commit no longer kept can't be compared: no badge, not a guess.
    const unknown = reasons({
      sha: NEW,
      citations: [{ sha: SHA, path: "backend/app/models.py", stale: null, reason: "unknown" }],
    });
    expect(staleFiles(cited, unknown)).toEqual([]);
    expect(staleFiles(cited, reasons(undefined))).toEqual([]);
  });

  it("says a view pinned to a commit the index moved on from is behind", () => {
    expect(pinnedBehind(SHA, NEW)).toBe(true);
    expect(pinnedBehind(SHA, SHA.toUpperCase())).toBe(false);
    expect(pinnedBehind("release", NEW)).toBe(false); // a branch follows its index
    expect(pinnedBehind(null, NEW)).toBe(false);
    expect(pinnedBehind(SHA, null)).toBe(false);
    expect(pinnedBehind("b1125d8", SHA)).toBe(false); // the first 7 characters pin it too
    expect(pinnedBehind("b1125d8", NEW)).toBe(true);
  });
});

describe("new commits indexed", () => {
  const update: RepoUpdate = {
    ref: "main",
    sha: NEW,
    changedFiles: ["backend/app/models.py"],
    changedCount: 1,
    at: 0,
  };

  it("is shown on pages read at its ref", () => {
    expect(concerns(update, null, "main")).toBe(true);
    expect(concerns(update, "main", "main")).toBe(true);
    expect(concerns(update, "release", "main")).toBe(false);
    expect(concerns({ ...update, ref: "release" }, "release", "main")).toBe(true);
    expect(concerns(update, SHA, "main")).toBe(true); // pinned: it offers the latest
    expect(concerns(update, "b1125d8", "main")).toBe(true);
  });

  it("keeps each ref's latest, so one ref's doesn't replace another's", () => {
    const { note, dismiss } = useRepoUpdates.getState();
    note("r", update);
    note("r", { ...update, ref: "pull/7", sha: SHA });
    expect(Object.keys(useRepoUpdates.getState().updates.r ?? {}).sort()).toEqual([
      "main",
      "pull/7",
    ]);
    dismiss("r", "pull/7");
    expect(useRepoUpdates.getState().updates.r?.main?.sha).toBe(NEW);
    expect(useRepoUpdates.getState().updates.r?.["pull/7"]).toBeUndefined();
  });

  it("says how much changed", () => {
    expect(updateLabel(update)).toBe("New commits indexed on main · 1 file changed");
    expect(updateLabel({ ...update, changedCount: 4 })).toBe(
      "New commits indexed on main · 4 files changed",
    );
    expect(updateLabel({ ...update, changedFiles: null, changedCount: null })).toBe(
      "New commits indexed on main",
    );
  });
});

describe("the status dot", () => {
  const job = (over: Partial<ActiveJob>): ActiveJob => ({
    id: "j",
    kind: "incremental",
    ref: null,
    status: "running",
    stage: "symbols",
    progress: 0.42,
    eta_s: null,
    ...over,
  });

  it("shows an incremental run's small %, the repository still readable", () => {
    const repo = { status: "indexing" as const, active_job: job({}) };
    expect(updatePercent(repo)).toBe(42);
    expect(describeStatus(repo).label).toBe("Updating 42%");
    expect(describeStatus({ ...repo, active_job: job({ ref: "release" }) }).label).toBe(
      "Updating release 42%",
    );
    expect(describeStatus({ ...repo, active_job: job({ status: "queued" }) }).label).toBe(
      "Update queued",
    );
  });

  it("leaves a full run as it was", () => {
    const repo = { status: "indexing" as const, active_job: job({ kind: "full" }) };
    expect(updatePercent(repo)).toBeNull();
    expect(describeStatus(repo).label).toBe("Indexing 42%");
    expect(updatePercent({ status: "ready", active_job: null })).toBeNull();
  });
});

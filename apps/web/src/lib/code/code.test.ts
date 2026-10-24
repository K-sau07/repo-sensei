import { describe, expect, it } from "vitest";

import type { TreeEntry } from "@/lib/api/types";
import { codePath } from "@/lib/routes";

import { languageName } from "./format";
import { clampLines, describeLines, extendLines, formatLines, parseLines } from "./lines";
import {
  ancestorsOf,
  flattenTree,
  layerOfRole,
  parentOf,
  treeKeyAction,
  typeAhead,
  type DirState,
} from "./tree";

describe("line ranges", () => {
  it("parses one line, a range, a reversed range and GitHub's form", () => {
    expect(parseLines("10")).toEqual({ start: 10, end: 10 });
    expect(parseLines("10-42")).toEqual({ start: 10, end: 42 });
    expect(parseLines("42-10")).toEqual({ start: 10, end: 42 });
    expect(parseLines("L10-L42")).toEqual({ start: 10, end: 42 });
  });

  it("rejects what isn't a range", () => {
    for (const bad of ["", "0", "0-3", "a", "10-", "-10", "1.5", "10-20-30", "1234567890"]) {
      expect(parseLines(bad)).toBeNull();
    }
  });

  it("writes ranges low to high, one line as a single number", () => {
    expect(formatLines({ start: 10, end: 10 })).toBe("10");
    expect(formatLines({ start: 10, end: 42 })).toBe("10-42");
    expect(describeLines({ start: 3, end: 3 })).toBe("Line 3");
    expect(describeLines({ start: 3, end: 9 })).toBe("Lines 3–9");
  });

  it("keeps a range inside the file", () => {
    expect(clampLines({ start: 30, end: 50 }, 36)).toEqual({ start: 30, end: 36 });
    expect(clampLines({ start: 40, end: 50 }, 36)).toBeNull();
  });

  it("grows a range from the line picked before with Shift", () => {
    expect(extendLines(null, 20, true)).toEqual({ start: 20, end: 20 });
    expect(extendLines({ start: 20, end: 20 }, 25, true)).toEqual({ start: 20, end: 25 });
    expect(extendLines({ start: 20, end: 25 }, 12, true)).toEqual({ start: 12, end: 25 });
    expect(extendLines({ start: 20, end: 25 }, 30, false)).toEqual({ start: 30, end: 30 });
  });
});

describe("code paths", () => {
  it("encodes each segment and keeps ref and lines", () => {
    expect(codePath("acme", "api")).toBe("/acme/api/code");
    expect(codePath("acme", "api", "backend/app/main.py")).toBe(
      "/acme/api/code/backend/app/main.py",
    );
    expect(codePath("acme", "api", "docs/a b#1.md", { ref: "b1125d8", lines: "10-42" })).toBe(
      "/acme/api/code/docs/a%20b%231.md?ref=b1125d8&L=10-42",
    );
  });
});

const dir = (path: string, childCount = 1): TreeEntry => ({
  name: path.slice(path.lastIndexOf("/") + 1),
  path,
  type: "dir",
  child_count: childCount,
});
const file = (path: string, role = "service"): TreeEntry => ({
  name: path.slice(path.lastIndexOf("/") + 1),
  path,
  type: "file",
  role,
});

const LISTINGS: Record<string, TreeEntry[]> = {
  "": [dir("backend"), dir("frontend"), file("README.md", "docs")],
  backend: [dir("backend/app"), file("backend/Dockerfile", "config")],
  "backend/app": [file("backend/app/main.py", "api"), file("backend/app/models.py", "db")],
};
const loaded = (path: string): DirState | undefined => {
  const entries = LISTINGS[path];
  return entries ? { status: "loaded", entries } : undefined;
};
const names = (rows: ReturnType<typeof flattenTree>) => rows.map((row) => row.entry.name);

describe("the file tree", () => {
  it("lists the root, then each open directory's children under it", () => {
    expect(names(flattenTree(loaded, new Set()))).toEqual(["backend", "frontend", "README.md"]);
    const rows = flattenTree(loaded, new Set(["backend", "backend/app"]));
    expect(names(rows)).toEqual([
      "backend",
      "app",
      "main.py",
      "models.py",
      "Dockerfile",
      "frontend",
      "README.md",
    ]);
    expect(rows.map((row) => row.level)).toEqual([1, 2, 3, 3, 2, 1, 1]);
    expect(rows[1]).toMatchObject({ position: 1, siblings: 2, expanded: true });
    expect(rows[6]).toMatchObject({ position: 3, siblings: 3 });
  });

  it("marks open directories still loading (or failed), and shows nothing before the root", () => {
    const rows = flattenTree(loaded, new Set(["frontend"]));
    expect(rows.find((row) => row.entry.name === "frontend")?.status).toBe("loading");
    const failed = flattenTree(
      (path) => (path === "frontend" ? { status: "error" } : loaded(path)),
      new Set(["frontend"]),
    );
    expect(failed.find((row) => row.entry.name === "frontend")?.status).toBe("error");
    expect(flattenTree(() => ({ status: "loading" }), new Set())).toEqual([]);
  });

  it("closes a collapsed directory's whole subtree, even with children still open", () => {
    expect(names(flattenTree(loaded, new Set(["backend/app"])))).toEqual([
      "backend",
      "frontend",
      "README.md",
    ]);
  });

  it("flattens a deep tree without recursion", () => {
    // 20,000 directories, each inside the one before (short made-up paths keep it small).
    const depth = 20_000;
    const paths = Array.from({ length: depth }, (_, i) => `d${String(i)}`);
    const deep = (path: string): DirState => {
      const child = paths[path === "" ? 0 : Number(path.slice(1)) + 1];
      return { status: "loaded", entries: child ? [dir(child)] : [] };
    };
    const rows = flattenTree(deep, new Set(paths));
    expect(rows).toHaveLength(depth);
    expect(rows.at(-1)?.level).toBe(depth);
  });

  it("knows a path's parent and the directories above it", () => {
    expect(parentOf("backend/app/main.py")).toBe("backend/app");
    expect(parentOf("README.md")).toBe("");
    expect(ancestorsOf("backend/app/main.py")).toEqual(["backend", "backend/app"]);
    expect(ancestorsOf("README.md")).toEqual([]);
  });

  it("moves through the rows the way the ARIA tree pattern says", () => {
    const rows = flattenTree(loaded, new Set(["backend"]));
    // backend, app, Dockerfile, frontend, README.md
    expect(treeKeyAction("ArrowDown", rows, 0)).toEqual({ type: "focus", index: 1 });
    expect(treeKeyAction("ArrowDown", rows, 4)).toBeNull();
    expect(treeKeyAction("ArrowUp", rows, 0)).toBeNull();
    expect(treeKeyAction("Home", rows, 3)).toEqual({ type: "focus", index: 0 });
    expect(treeKeyAction("End", rows, 0)).toEqual({ type: "focus", index: 4 });
    // → opens a closed directory, then moves into it; on a file it does nothing.
    expect(treeKeyAction("ArrowRight", rows, 1)).toEqual({ type: "expand", path: "backend/app" });
    expect(treeKeyAction("ArrowRight", rows, 0)).toEqual({ type: "focus", index: 1 });
    expect(treeKeyAction("ArrowRight", rows, 4)).toBeNull();
    // ← closes an open directory, otherwise goes to the parent.
    expect(treeKeyAction("ArrowLeft", rows, 0)).toEqual({ type: "collapse", path: "backend" });
    expect(treeKeyAction("ArrowLeft", rows, 2)).toEqual({ type: "focus", index: 0 });
    expect(treeKeyAction("ArrowLeft", rows, 3)).toBeNull();
    expect(treeKeyAction("Enter", rows, 2)).toEqual({ type: "activate", index: 2 });
    expect(treeKeyAction(" ", rows, 2)).toEqual({ type: "activate", index: 2 });
    expect(treeKeyAction("x", rows, 2)).toBeNull();
  });

  it("type-ahead finds the next name with the prefix, wrapping, and cycles on one letter", () => {
    const rows = flattenTree(loaded, new Set(["backend", "backend/app"]));
    // backend, app, main.py, models.py, Dockerfile, frontend, README.md
    expect(typeAhead(rows, 0, "f")).toBe(5);
    expect(typeAhead(rows, 0, "m")).toBe(2);
    expect(typeAhead(rows, 2, "m")).toBe(3); // the same letter again: the next one
    expect(typeAhead(rows, 3, "mm")).toBe(2); // and around
    expect(typeAhead(rows, 2, "ma")).toBe(2); // a longer prefix keeps a row that still matches
    expect(typeAhead(rows, 2, "mo")).toBe(3);
    expect(typeAhead(rows, 6, "B")).toBe(0); // case-insensitive, wrapping
    expect(typeAhead(rows, 0, "zz")).toBe(-1);
  });

  it("colors files by the layer their role puts them in", () => {
    expect(layerOfRole("ui")).toBe("ui");
    expect(layerOfRole("api")).toBe("api");
    expect(layerOfRole("service")).toBe("svc");
    expect(layerOfRole("db")).toBe("db");
    for (const role of ["test", "config", "docs", "other", null])
      expect(layerOfRole(role)).toBeNull();
  });
});

describe("language names", () => {
  it("names the engine's language ids", () => {
    expect(languageName("python")).toBe("Python");
    expect(languageName("tsx")).toBe("TypeScript (TSX)");
    expect(languageName("pip-requirements")).toBe("Pip requirements");
    expect(languageName("cobol")).toBe("cobol");
    expect(languageName(null)).toBeNull();
  });
});

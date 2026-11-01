import { describe, expect, it } from "vitest";

import { folderGroups } from "./handlers/galaxy";

// The engine's cases (packages/engine/tests/test_galaxy.py): the mock groups as the API does.
const named = (paths: string[], options?: Parameters<typeof folderGroups>[1]) =>
  folderGroups(paths, options).map(([label, members]) => [label, members.length]);

const PATHS = [
  ...Array.from({ length: 40 }, (_, i) => `src/web/app/f${String(i)}.ts`),
  ...Array.from({ length: 30 }, (_, i) => `src/api/m${String(i)}.py`),
  ...Array.from({ length: 2 }, (_, i) => `src/f${String(i)}.py`),
  ...Array.from({ length: 20 }, (_, i) => `docs/d${String(i)}.md`),
  "README.md",
  "tools/a.sh",
  "ci/b.yml",
];

describe("the galaxy by folder", () => {
  it("shows a big folder as its parts, the smallest together", () => {
    expect(named(PATHS, { small: 0.03 })).toEqual([
      ["src/web/app", 40],
      ["src/api", 30],
      ["docs", 20],
      ["(the rest)", 5],
    ]);
    const every = folderGroups(PATHS).flatMap(([, members]) => members);
    expect(every.sort((a, b) => a - b)).toEqual(PATHS.map((_, i) => i));
    expect(named(PATHS, { small: 0.02, most: 4 })).toEqual([
      ["src/web/app", 40],
      ["src", 32],
      ["docs", 20],
      ["(the rest)", 3],
    ]);
  });

  it("keeps a small repository's top folders", () => {
    const paths = [
      ...Array.from({ length: 4 }, (_, i) => `backend/app/f${String(i)}.py`),
      "README.md",
    ];
    expect(named(paths)).toEqual([
      ["backend", 4],
      ["(root)", 1],
    ]);
  });
});

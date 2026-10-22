import { describe, expect, it } from "vitest";

import { fileDetails, githubBlobUrl, withinFile } from "./file-view";

describe("the lines a link picks", () => {
  const content = "one\ntwo\nthree\n"; // four lines, as the editor counts them
  it.each([
    [null, null],
    [
      { start: 2, end: 3 },
      { start: 2, end: 3 },
    ],
    [
      { start: 3, end: 40 },
      { start: 3, end: 4 },
    ],
    [{ start: 500, end: 500 }, null],
  ])("%o in a four-line file is %o", (lines, shown) => {
    expect(withinFile(lines, content)).toEqual(shown);
  });
});

describe("links to GitHub", () => {
  it("encodes each path and ref segment, keeping a branch's slashes", () => {
    expect(githubBlobUrl("acme-gh/api", "feat/a#2", "docs/my notes#1.md", null)).toBe(
      "https://github.com/acme-gh/api/blob/feat/a%232/docs/my%20notes%231.md",
    );
    expect(githubBlobUrl("acme-gh/api", "abc1234", "a.py", { start: 3, end: 3 })).toBe(
      "https://github.com/acme-gh/api/blob/abc1234/a.py#L3",
    );
    expect(githubBlobUrl("acme-gh/api", "abc1234", "a.py", { start: 3, end: 9 })).toBe(
      "https://github.com/acme-gh/api/blob/abc1234/a.py#L3-L9",
    );
  });
});

describe("what a 413 or 415 says", () => {
  it("reads the details it needs and ignores the rest", () => {
    expect(
      fileDetails({ path: "a.png", size: 12, language: null, download_url: "https://x" }),
    ).toMatchObject({ path: "a.png", size: 12, download_url: "https://x", commit_sha: null });
    expect(fileDetails({ path: "a.png" })).toBeNull();
    expect(fileDetails("nope")).toBeNull();
  });
});

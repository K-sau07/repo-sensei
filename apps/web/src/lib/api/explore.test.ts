import { describe, expect, it } from "vitest";

import {
  askFailure,
  githubBlob,
  githubTree,
  languageShares,
  mapSymbols,
  symbolName,
} from "./explore";

describe("the public gallery's helpers", () => {
  it("links a symbol's lines on GitHub at the commit, each path segment encoded", () => {
    expect(githubBlob("o/r", "abc", "src/a b/#x.py", [3, 9])).toBe(
      "https://github.com/o/r/blob/abc/src/a%20b/%23x.py#L3-L9",
    );
    expect(githubBlob("o/r", "abc", "a.py", [4, 4])).toBe(
      "https://github.com/o/r/blob/abc/a.py#L4",
    );
    expect(githubTree("o/r", "abc", ".")).toBe("https://github.com/o/r/tree/abc");
    expect(githubTree("o/r", "abc", "backend/app")).toBe(
      "https://github.com/o/r/tree/abc/backend/app",
    );
  });

  it("names a symbol by its key, as the feature maps read it", () => {
    expect(symbolName("py a/b.py/Item#save().")).toBe("Item.save");
    expect(symbolName("py backend/app/api/deps.py/get_current_user().")).toBe("get_current_user");
  });

  it("keeps only the map's symbols that point at lines", () => {
    const found = mapSymbols([
      { symbol_key: "py a.py/f().", why: "starts it", path: "a.py", start_line: 2, end_line: 5 },
      { symbol_key: "py b.py/g().", path: "b.py", start_line: 0, end_line: 1 }, // no line 0
      { symbol_key: "py c.py/h().", path: "", start_line: 1, end_line: 2 }, // no file
      { symbol_key: "py d.py/k().", path: "d.py", start_line: 9, end_line: 3 }, // backwards
    ]);
    expect(found).toEqual([
      { symbolKey: "py a.py/f().", name: "f", why: "starts it", path: "a.py", lines: [2, 5] },
    ]);
  });

  it("shares a repository's code out by language, the rest as Other", () => {
    expect(languageShares({})).toEqual([]);
    const shares = languageShares({ Go: 10, Python: 60, Shell: 5, C: 25 }, 2);
    expect(shares).toEqual([
      { language: "Python", share: 0.6 },
      { language: "C", share: 0.25 },
      { language: "Other", share: 0.15 },
    ]);
  });

  it("says what the API said when a question is refused", () => {
    const refused = new Error(
      JSON.stringify({ error: { code: "quota_exceeded", message: "Later." } }),
    );
    expect(askFailure(refused)).toBe("Later.");
    expect(askFailure(new Error("Failed to fetch"))).toMatch(/couldn't be written/);
  });
});

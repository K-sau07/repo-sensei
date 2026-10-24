import { createLoader } from "nuqs";
import { describe, expect, it } from "vitest";

import {
  fromCharacterColumns,
  markTokens,
  normalizeRanges,
  rangesOnLine,
  segments,
  windowLine,
} from "./highlight";
import { EMPTY_SEARCH, filterCount, requestOf, searchParsers, searchPath } from "./url-state";

const marked = (text: string, ranges: { start: number; end: number }[]) =>
  segments(text, ranges)
    .map((part) => (part.mark ? `[${part.text}]` : part.text))
    .join("");

describe("highlight ranges", () => {
  it("marks UTF-16 ranges as text, in order", () => {
    expect(marked("def create_user(", [{ start: 4, end: 15 }])).toBe("def [create_user](");
    expect(
      marked("a b c", [
        { start: 4, end: 5 },
        { start: 0, end: 1 },
      ]),
    ).toBe("[a] b [c]");
    expect(segments("", [])).toEqual([{ text: "", mark: false }]);
  });

  it("merges overlapping and touching ranges", () => {
    expect(
      marked("password_hash", [
        { start: 0, end: 8 },
        { start: 4, end: 13 },
      ]),
    ).toBe("[password_hash]");
    expect(
      marked("email", [
        { start: 0, end: 2 },
        { start: 2, end: 5 },
      ]),
    ).toBe("[email]");
    expect(
      normalizeRanges("mail", [
        { start: 1, end: 3 },
        { start: 0, end: 4 },
      ]),
    ).toEqual([{ start: 0, end: 4 }]);
  });

  it("clips ranges past either end and drops empty or reversed ones", () => {
    expect(marked("short", [{ start: 3, end: 99 }])).toBe("sho[rt]");
    expect(marked("short", [{ start: -4, end: 2 }])).toBe("[sh]ort");
    expect(marked("short", [{ start: 9, end: 12 }])).toBe("short");
    expect(
      marked("short", [
        { start: 3, end: 3 },
        { start: 4, end: 2 },
      ]),
    ).toBe("short");
    expect(marked("short", [{ start: Number.NaN, end: 2 }])).toBe("short");
  });

  it("counts UTF-16 units, and never cuts a character in two", () => {
    // "🚀" is two UTF-16 units: "Deploy" starts at 3.
    const text = "🚀 Deploy";
    expect(marked(text, [{ start: 3, end: 9 }])).toBe("🚀 [Deploy]");
    // A range ending (or starting) inside the emoji takes the whole emoji.
    expect(marked(text, [{ start: 0, end: 1 }])).toBe("[🚀] Deploy");
    expect(marked(text, [{ start: 1, end: 3 }])).toBe("[🚀 ]Deploy");
  });

  it("picks one line's ranges from a snippet", () => {
    const highlights = [
      { line: 10, start: 0, end: 3 },
      { line: 11, start: 2, end: 4 },
      { line: 10, start: 5, end: 6 },
    ];
    expect(rangesOnLine(highlights, 10)).toEqual([
      { start: 0, end: 3 },
      { start: 5, end: 6 },
    ]);
  });

  it("cuts a long line around its first mark", () => {
    const text = `${"x".repeat(1000)}NEEDLE${"y".repeat(1000)}`;
    const shown = windowLine(text, [{ start: 1000, end: 1006 }], 100);
    expect(shown.text.length).toBe(100);
    expect(shown.cutStart && shown.cutEnd).toBe(true);
    const [mark] = shown.ranges;
    expect(mark && shown.text.slice(mark.start, mark.end)).toBe("NEEDLE");
    expect(windowLine("short", [], 100)).toMatchObject({ text: "short", cutStart: false });
  });

  it("converts exact search's character columns, after its offset", () => {
    // Columns are 1-based characters of the whole line; the emoji is one character.
    expect(fromCharacterColumns("🚀 Deploy now", 0, [[3, 9]])).toEqual([{ start: 3, end: 9 }]);
    // The text starts 10 characters into the line.
    expect(fromCharacterColumns("abcdef", 10, [[12, 14]])).toEqual([{ start: 1, end: 3 }]);
    expect(fromCharacterColumns("abc", 0, [[2, 99]])).toEqual([{ start: 1, end: 3 }]);
  });

  it("splits syntax tokens where marks start and end", () => {
    const tokens = [
      { text: "def", tag: "keyword" },
      { text: " ", tag: null },
      { text: "create_user", tag: "function" },
    ];
    expect(markTokens(tokens, [{ start: 2, end: 8 }])).toEqual([
      { text: "de", tag: "keyword", mark: false },
      { text: "f", tag: "keyword", mark: true },
      { text: " ", tag: null, mark: true },
      { text: "crea", tag: "function", mark: true },
      { text: "te_user", tag: "function", mark: false },
    ]);
  });
});

describe("the search URL", () => {
  const load = createLoader(searchParsers);

  it("round-trips the query and every filter", () => {
    const state = {
      q: "how are passwords hashed?",
      layer: ["api" as const, "db" as const],
      lang: ["python", "typescript"],
      path: "backend/app/",
      ref: "main",
      mode: "keyword" as const,
    };
    const href = searchPath("acme", "api", state);
    expect(href.startsWith("/acme/api/search?")).toBe(true);
    const url = new URL(href, "http://localhost");
    expect(load(url)).toEqual(state);
  });

  it("leaves defaults out, and reads a bare page as the empty search", () => {
    expect(searchPath("acme", "api", { q: "login" })).toBe("/acme/api/search?q=login");
    expect(searchPath("acme", "api")).toBe("/acme/api/search");
    expect(load(new URL("http://localhost/acme/api/search"))).toEqual(EMPTY_SEARCH);
  });

  it("drops a layer or mode it doesn't know", () => {
    const url = new URL("http://localhost/acme/api/search?q=x&layer=api,service&mode=fuzzy");
    expect(load(url)).toMatchObject({ q: "x", layer: ["api"], mode: null });
  });

  it("becomes the request body, filters only when set", () => {
    expect(requestOf({ ...EMPTY_SEARCH, q: "  login " })).toEqual({ q: "login" });
    expect(
      requestOf({
        q: "login",
        layer: ["ui"],
        lang: ["tsx"],
        path: "frontend/",
        ref: "abc1234",
        mode: "keyword",
      }),
    ).toEqual({
      q: "login",
      layers: ["ui"],
      langs: ["tsx"],
      path: "frontend/",
      ref: "abc1234",
      mode: "keyword",
    });
    expect(requestOf({ ...EMPTY_SEARCH, q: "a+", mode: "regex" })).toEqual({ q: "a+" });
    expect(filterCount({ ...EMPTY_SEARCH, layer: ["ui", "api"], ref: "main" })).toBe(2);
  });
});

import { describe, expect, it } from "vitest";

import { matchItems } from "@/lib/palette/match";

import { interimMatches } from "./use-worker-matches";

const ITEMS = [
  { id: "a", title: "login.py" },
  { id: "b", title: "logs.py" },
  { id: "c", title: "blog.md" },
  { id: "d", title: "main.py" },
];

describe("interimMatches (while the worker works on the new query)", () => {
  it("is the worker's answer itself once it's for this query", () => {
    const answer = { query: "log", items: ITEMS, matches: matchItems("log", ITEMS) };
    expect(interimMatches(answer, "log")).toBe(answer.matches);
  });

  it("keeps only the previous rows that still match what's typed, scored for it", () => {
    const answer = { query: "log", items: ITEMS, matches: matchItems("log", ITEMS) };
    expect(answer.matches.map((match) => match.id)).toEqual(["a", "b", "c"]);
    const interim = interimMatches(answer, "logi");
    expect(interim.map((match) => match.id)).toEqual(["a"]);
    expect(interim).toEqual(matchItems("logi", [ITEMS[0] ?? { id: "", title: "" }]));
  });

  it("never brings in rows the previous answer didn't have", () => {
    const answer = { query: "logs", items: ITEMS, matches: matchItems("logs", ITEMS) };
    expect(interimMatches(answer, "l").map((match) => match.id)).toEqual(["b"]);
  });
});
